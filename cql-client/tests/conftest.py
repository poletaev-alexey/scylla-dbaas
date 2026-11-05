"""An in-memory stand-in for a scylla-driver Session (no Cluster needed)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cql_client.connection import ConnectionSettings
from cql_client.workload import INSERT_CQL, SELECT_ONE_CQL, SELECT_SOME_CQL


class _Future:
    def __init__(self, value: Any) -> None:
        self.value = value

    def result(self) -> Any:
        return self.value


class FakeSession:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, tuple[datetime, str]] = {}
        self.ddl: list[str] = []
        self.prepared: list[str] = []
        self.async_calls = 0

    def prepare(self, cql: str) -> str:
        self.prepared.append(cql)
        return cql

    def execute(self, stmt: str, params: tuple[Any, ...] | None = None) -> list[Any]:
        if stmt.startswith("CREATE"):
            self.ddl.append(stmt)
            return []
        if stmt == SELECT_SOME_CQL:
            assert params is not None
            items = list(self.rows.items())[: params[0]]
            return [SimpleNamespace(id=k, ts=v[0], payload=v[1]) for k, v in items]
        if stmt == SELECT_ONE_CQL:
            assert params is not None
            v = self.rows.get(params[0])
            return [] if v is None else [SimpleNamespace(id=params[0], ts=v[0], payload=v[1])]
        if stmt == INSERT_CQL:
            assert params is not None
            row_id, ts, payload = params
            # Scylla stores ms precision and the driver returns naive UTC datetimes.
            self.rows[row_id] = (ts.replace(tzinfo=None), payload)
            return []
        raise AssertionError(f"unexpected statement {stmt}")

    def execute_async(self, stmt: str, params: tuple[Any, ...]) -> _Future:
        self.async_calls += 1
        return _Future(self.execute(stmt, params))


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def connector(session: FakeSession) -> Any:
    seen: list[ConnectionSettings] = []

    @contextmanager
    def _connect(cfg: ConnectionSettings) -> Iterator[tuple[FakeSession, str]]:
        seen.append(cfg)
        yield session, cfg.dc or "datacenter1"

    _connect.seen = seen  # type: ignore[attr-defined]
    return _connect
