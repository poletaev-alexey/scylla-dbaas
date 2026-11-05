"""End-to-end lifecycle on the ``local`` driver: real ScyllaDB, Scylla Manager and MinIO
containers, driven only through the public API.

    cd control-plane && DBAAS_INTEGRATION=1 .venv/bin/pytest -m integration tests/integration

Needs a running Docker daemon, ~6 GB RAM and 15-30 minutes (two Clusters are created).
Data is written and read with ``cqlsh`` inside a Data Node container, so the test does not
depend on the CQL client package.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from dbaas.config import Settings
from dbaas.main import create_app
from dbaas.operations.runner import OperationRunner
from dbaas.providers.local import LocalInfrastructure, build

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DBAAS_INTEGRATION") != "1", reason="set DBAAS_INTEGRATION=1 (needs Docker)"
    ),
]

HEADERS = {"X-API-Key": "it-key"}
ROWS = [(i, f"row-{i}") for i in range(1, 21)]


async def cqlsh(cluster_id: str, cql: str) -> str:
    container = LocalInfrastructure.container_name(cluster_id, "data-1")
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container, "cqlsh", "-e", cql,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode()


class Api:
    def __init__(self, client: httpx.AsyncClient, runner: OperationRunner) -> None:
        self.client = client
        self.runner = runner

    async def call(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.client.request(method, f"/api/v1{path}", **kw)
        assert r.status_code < 300, r.text
        return r.json()

    async def run(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        """Submit a mutating call and wait for its Operation to succeed."""
        accepted: dict[str, Any] = await self.call(method, path, **kw)
        await self.runner.wait_all()
        op: dict[str, Any] = await self.call("GET", f"/operations/{accepted['operation']['id']}")
        log = "\n".join(e["message"] for e in op.get("log", []))
        assert op["status"] == "succeeded", f"{op['type']} failed: {op.get('error')}\n{log}"
        return accepted


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/it.db",
        api_keys="it-key:integration",
        provider="local",
    )
    app = create_app(settings, build(settings), start_health_checker=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://it", headers=HEADERS, timeout=60
        ) as client:
            yield Api(client, app.state.runner)


async def test_create_backup_delete_restore(api: Api) -> None:
    created: list[str] = []
    try:
        # 1. create
        src = (await api.run("POST", "/clusters", json={"name": "it-source"}))["cluster"]["id"]
        created.append(src)
        cluster = await api.call("GET", f"/clusters/{src}")
        assert cluster["status"] == "healthy"
        conn = await api.call("GET", f"/clusters/{src}/connection")
        assert all(p.startswith("127.0.0.1:") for p in conn["contact_points"])

        # 2. write data, take a Backup
        await cqlsh(
            src,
            "CREATE KEYSPACE it WITH replication = "
            "{'class': 'NetworkTopologyStrategy', 'replication_factor': 3};"
            "CREATE TABLE it.kv (id int PRIMARY KEY, v text);"
            + "".join(f"INSERT INTO it.kv (id, v) VALUES ({i}, '{v}');" for i, v in ROWS),
        )
        await api.run("POST", f"/clusters/{src}/backups")
        backups = await api.call("GET", f"/clusters/{src}/backups")
        assert backups[0]["status"] == "done" and backups[0]["snapshot_tag"]
        backup_id = backups[0]["id"]

        # 3. delete the source Cluster; the Backup must survive it
        await api.run("DELETE", f"/clusters/{src}")
        created.remove(src)
        assert (await api.call("GET", f"/backups/{backup_id}"))["status"] == "done"

        # 4. Restore into a new Cluster and read the data back
        dst = (
            await api.run(
                "POST", "/clusters", json={"name": "it-restored", "from_backup_id": backup_id}
            )
        )["cluster"]["id"]
        created.append(dst)
        out = await cqlsh(dst, "SELECT id, v FROM it.kv;")
        for i, v in ROWS:
            assert v in out, f"row {i} missing after Restore:\n{out}"

        # 5. clean up through the API, including the Backups
        await api.run("DELETE", f"/clusters/{dst}")
        created.remove(dst)
        await api.run("DELETE", f"/clusters/{src}/backups")
    finally:
        infra = LocalInfrastructure()

        async def nolog(_: str) -> None:
            return None

        for cid in created:  # best-effort cleanup after a failure
            await infra.destroy(cid, nolog)
