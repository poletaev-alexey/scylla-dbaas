import uuid
from pathlib import Path

import pytest
from conftest import FakeSession

from cql_client.workload import (
    INSERT_CQL,
    Manifest,
    keyspace_cql,
    read_rows,
    verify_rows,
    write_rows,
)


def test_keyspace_uses_network_topology_rf3_with_given_dc() -> None:
    cql = keyspace_cql("eu-central")
    assert "'class': 'NetworkTopologyStrategy'" in cql
    assert "'eu-central': 3" in cql
    assert "IF NOT EXISTS demo" in cql


def test_keyspace_rejects_injection_in_dc_name() -> None:
    with pytest.raises(ValueError):
        keyspace_cql("dc1': 1}; DROP KEYSPACE demo; --")


def test_write_creates_schema_uses_prepared_statement_and_concurrency(
    session: FakeSession,
) -> None:
    m = write_rows(session, 10, "datacenter1", concurrency=3)
    assert len(m.rows) == 10 and len(session.rows) == 10
    assert any("NetworkTopologyStrategy" in d for d in session.ddl)
    assert any("CREATE TABLE IF NOT EXISTS demo.kv" in d for d in session.ddl)
    assert session.prepared == [INSERT_CQL]
    assert session.async_calls == 10
    assert m.datacenter == "datacenter1"


def test_write_rejects_zero(session: FakeSession) -> None:
    with pytest.raises(ValueError):
        write_rows(session, 0, "dc")


def test_read_returns_rows(session: FakeSession) -> None:
    write_rows(session, 5, "dc")
    rows = read_rows(session, 3)
    assert len(rows) == 3
    assert all(p.startswith("row-") for _, _, p in rows)


def test_verify_ok_after_write(session: FakeSession) -> None:
    m = write_rows(session, 20, "dc")
    res = verify_rows(session, m)
    assert res.ok and res.checked == 20


def test_verify_detects_missing_and_mismatched(session: FakeSession) -> None:
    m = write_rows(session, 4, "dc")
    first, second = uuid.UUID(m.rows[0].id), uuid.UUID(m.rows[1].id)
    del session.rows[first]
    ts, _ = session.rows[second]
    session.rows[second] = (ts, "tampered")
    res = verify_rows(session, m)
    assert not res.ok
    assert res.missing == [str(first)]
    assert res.mismatched == [str(second)]


def test_verify_of_empty_manifest_is_not_ok(session: FakeSession) -> None:
    assert not verify_rows(session, Manifest()).ok


def test_manifest_roundtrip(tmp_path: Path, session: FakeSession) -> None:
    m = write_rows(session, 3, "eu-central")
    path = tmp_path / "m.json"
    m.save(path)
    assert Manifest.load(path) == m


def test_manifest_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text('{"version": 99, "rows": []}')
    with pytest.raises(ValueError):
        Manifest.load(path)
