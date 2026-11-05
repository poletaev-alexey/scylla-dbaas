"""The end-to-end scenario against an in-memory fake of the Control Plane API."""

import itertools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cql_client.api import DbaasApi, OperationFailed
from cql_client.e2e import Scenario


class FakeControlPlane:
    def __init__(self, fail_backup: bool = False) -> None:
        self.ids = itertools.count(1)
        self.clusters: dict[str, dict[str, Any]] = {}
        self.ops: dict[str, dict[str, Any]] = {}
        self.fail_backup = fail_backup
        self.polls = 0
        self.backups_deleted: list[str] = []

    def _op(self, type_: str, status: str = "succeeded", **params: Any) -> dict[str, Any]:
        op_id = f"op-{next(self.ids)}"
        self.ops[op_id] = {
            "id": op_id,
            "type": type_,
            "status": "running",
            "final": status,
            "params": params,
            "error": None if status == "succeeded" else "boom",
            "log": [{"message": f"{type_} step"}],
        }
        return self.ops[op_id]

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path.removeprefix("/api/v1"), req.method
        if m == "POST" and path == "/clusters":
            body = json.loads(req.content)
            cid = f"c-{next(self.ids)}"
            self.clusters[cid] = {"id": cid, "name": body["name"], **body}
            return httpx.Response(
                202, json={"operation": self._op("create"), "cluster": {"id": cid}}
            )
        if m == "DELETE" and path.endswith("/backups"):
            self.backups_deleted.append(path.split("/")[2])
            return httpx.Response(202, json={"operation": self._op("delete_backups")})
        if m == "DELETE" and path.startswith("/clusters/"):
            cid = path.split("/")[2]
            assert req.url.params["confirm"] == self.clusters[cid]["name"]
            self.clusters[cid]["deleted"] = True
            return httpx.Response(202, json={"operation": self._op("delete")})
        if m == "POST" and path.endswith("/backups"):
            status = "failed" if self.fail_backup else "succeeded"
            return httpx.Response(
                202, json={"operation": self._op("backup", status, backup_id="b-1")}
            )
        if m == "GET" and path.startswith("/operations/"):
            op = self.ops[path.split("/")[2]]
            self.polls += 1
            if self.polls % 2 == 0:  # every op needs one extra poll
                op["status"] = op["final"]
            return httpx.Response(200, json=op)
        if m == "GET" and path.endswith("/connection"):
            return httpx.Response(
                200, json={"contact_points": ["h"], "port": 9042, "username": "u", "password": "p"}
            )
        if m == "GET" and path.startswith("/clusters/"):
            return httpx.Response(200, json={"nodes": []})
        return httpx.Response(404, json={"detail": f"{m} {path}"})


def _scenario(cp: FakeControlPlane, connector: Any, state: Path) -> Scenario:
    api = DbaasApi("http://cp", "k", transport=httpx.MockTransport(cp.handler))
    return Scenario(api, connector=connector, poll=0, state_file=state, log=lambda _m: None)


def test_full_scenario_with_restore_and_cleanup(connector: Any, tmp_path: Path) -> None:
    cp = FakeControlPlane()
    sc = _scenario(cp, connector, tmp_path / "state")
    sc.run("e2e-x", rows=5, with_restore=True)
    restored = next(c for c in cp.clusters.values() if c["name"] == "e2e-x-r")
    assert restored["from_backup_id"] == "b-1"
    assert sc.cleanup()
    assert all(c.get("deleted") for c in cp.clusters.values())
    assert len((tmp_path / "state").read_text().splitlines()) == 2
    source = next(cid for cid, c in cp.clusters.items() if c["name"] == "e2e-x")
    assert cp.backups_deleted == [source]


def test_failed_backup_raises_and_cleanup_still_deletes(connector: Any, tmp_path: Path) -> None:
    cp = FakeControlPlane(fail_backup=True)
    sc = _scenario(cp, connector, tmp_path / "state")
    with pytest.raises(OperationFailed):
        sc.run("e2e-y", rows=2, with_restore=True)
    assert sc.cleanup()
    assert all(c.get("deleted") for c in cp.clusters.values())


def test_wait_operation_times_out() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "op", "status": "running", "log": []})

    api = DbaasApi("http://cp", "k", transport=httpx.MockTransport(handler))
    t = iter(range(100))
    with pytest.raises(TimeoutError):
        api.wait_operation("op", timeout=3, sleep=lambda _s: None, clock=lambda: next(t))
