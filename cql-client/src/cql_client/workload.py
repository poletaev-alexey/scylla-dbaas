"""Demo workload: write rows, read them back, verify them after a Restore.

Schema (created idempotently by ``write``)::

    CREATE KEYSPACE demo WITH replication =
        {'class': 'NetworkTopologyStrategy', '<dc>': 3};
    CREATE TABLE demo.kv (id uuid PRIMARY KEY, ts timestamp, payload text);

Every written row is recorded in a manifest (JSON: id, timestamp in ms, SHA-256 of the
payload). ``verify`` re-reads exactly those rows, so it proves a restored Cluster holds the
data that was written before the Backup.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KEYSPACE = "demo"
TABLE = "kv"
REPLICATION_FACTOR = 3
MANIFEST_VERSION = 1

_DC_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def keyspace_cql(dc: str, rf: int = REPLICATION_FACTOR) -> str:
    if not _DC_RE.match(dc):
        raise ValueError(f"invalid datacenter name {dc!r}")
    return (
        f"CREATE KEYSPACE IF NOT EXISTS {KEYSPACE} WITH replication = "
        f"{{'class': 'NetworkTopologyStrategy', '{dc}': {rf}}}"
    )


TABLE_CQL = (
    f"CREATE TABLE IF NOT EXISTS {KEYSPACE}.{TABLE} "
    "(id uuid PRIMARY KEY, ts timestamp, payload text)"
)
INSERT_CQL = f"INSERT INTO {KEYSPACE}.{TABLE} (id, ts, payload) VALUES (?, ?, ?)"
SELECT_ONE_CQL = f"SELECT id, ts, payload FROM {KEYSPACE}.{TABLE} WHERE id = ?"
SELECT_SOME_CQL = f"SELECT id, ts, payload FROM {KEYSPACE}.{TABLE} LIMIT ?"


def _sha(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def _ms(ts: datetime) -> int:
    # The driver returns naive datetimes in UTC.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return int(ts.timestamp() * 1000)


@dataclass(frozen=True)
class ManifestRow:
    id: str
    ts_ms: int
    payload_sha256: str


@dataclass
class Manifest:
    keyspace: str = KEYSPACE
    table: str = TABLE
    datacenter: str = ""
    rows: list[ManifestRow] = field(default_factory=list)

    def save(self, path: Path) -> None:
        data = {
            "version": MANIFEST_VERSION,
            "keyspace": self.keyspace,
            "table": self.table,
            "datacenter": self.datacenter,
            "rows": [r.__dict__ for r in self.rows],
        }
        path.write_text(json.dumps(data, indent=1) + "\n")

    @classmethod
    def load(cls, path: Path) -> Manifest:
        data = json.loads(path.read_text())
        if data.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version in {path}")
        return cls(
            keyspace=data["keyspace"],
            table=data["table"],
            datacenter=data.get("datacenter", ""),
            rows=[ManifestRow(**r) for r in data["rows"]],
        )


def _run_concurrent(
    session: Any, statement: Any, params: Sequence[tuple[Any, ...]], concurrency: int
) -> list[Any]:
    """Execute ``statement`` for each parameter tuple, ``concurrency`` requests in flight.
    Returns the result of every request in order; the first error propagates."""
    results: list[Any] = []
    for start in range(0, len(params), concurrency):
        futures = [session.execute_async(statement, p) for p in params[start : start + concurrency]]
        results.extend(f.result() for f in futures)
    return results


def ensure_schema(session: Any, dc: str) -> None:
    session.execute(keyspace_cql(dc))
    session.execute(TABLE_CQL)


def write_rows(
    session: Any,
    count: int,
    dc: str,
    *,
    concurrency: int = 64,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Manifest:
    if count < 1:
        raise ValueError("count must be >= 1")
    ensure_schema(session, dc)
    insert = session.prepare(INSERT_CQL)
    rows: list[ManifestRow] = []
    params: list[tuple[Any, ...]] = []
    for i in range(count):
        row_id = uuid.uuid4()
        ts = now()
        ts = ts.replace(microsecond=ts.microsecond // 1000 * 1000)  # CQL timestamp is ms
        payload = f"row-{i}-{secrets.token_hex(16)}"
        params.append((row_id, ts, payload))
        rows.append(ManifestRow(str(row_id), _ms(ts), _sha(payload)))
    _run_concurrent(session, insert, params, concurrency)
    return Manifest(datacenter=dc, rows=rows)


def read_rows(session: Any, limit: int) -> list[tuple[str, datetime, str]]:
    select = session.prepare(SELECT_SOME_CQL)
    return [(str(r.id), r.ts, r.payload) for r in session.execute(select, (limit,))]


@dataclass
class VerifyResult:
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.checked > 0 and not self.missing and not self.mismatched


def verify_rows(session: Any, manifest: Manifest, *, concurrency: int = 64) -> VerifyResult:
    select = session.prepare(SELECT_ONE_CQL)
    params = [(uuid.UUID(r.id),) for r in manifest.rows]
    results = _run_concurrent(session, select, params, concurrency)
    out = VerifyResult(checked=len(manifest.rows))
    for expected, result in zip(manifest.rows, results, strict=True):
        found: Iterable[Any] = list(result)
        row = next(iter(found), None)
        if row is None:
            out.missing.append(expected.id)
        elif _ms(row.ts) != expected.ts_ms or _sha(row.payload) != expected.payload_sha256:
            out.mismatched.append(expected.id)
    return out
