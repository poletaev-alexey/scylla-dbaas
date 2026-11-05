"""ManagerClusterOps, Manager/REST clients and the local driver against mocked HTTP/docker."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from dbaas.domain import NodeRole
from dbaas.providers.base import ClusterInfra, InstanceState, NodeInfo, SnapshotRef
from dbaas.providers.local import DockerResult, LocalInfrastructure, LocalSettings, rest_endpoint
from dbaas.scylla.manager import ManagerError
from dbaas.scylla.ops import (
    ManagerClusterOps,
    backup_prefixes,
    cron_for_interval,
    manager_cluster_uuid,
    parse_backup_location,
)
from dbaas.scylla.rest import ScyllaRestClient

MANAGER = "10.0.1.100"
DATA_IPS = ["10.0.1.1", "10.0.1.2", "10.0.1.3"]
LOCATION = "s3:dbaas-backups/clusters/c-1234"


def make_infra() -> ClusterInfra:
    nodes = [
        NodeInfo(f"data-{i + 1}", NodeRole.DATA, f"i-{i}", InstanceState.RUNNING, ip, ip)
        for i, ip in enumerate(DATA_IPS)
    ]
    nodes.append(
        NodeInfo("monitor", NodeRole.MONITOR, "i-m", InstanceState.RUNNING, MANAGER, MANAGER)
    )
    return ClusterInfra(
        nodes=nodes,
        grafana_url=f"http://{MANAGER}:3000",
        manager_url=f"http://{MANAGER}:5080",
        credentials_ref="test:c-1234",
        backup_location=LOCATION,
    )


@dataclass
class FakeWorld:
    """Scylla Manager + three Scylla REST APIs behind one MockTransport."""

    manager_up: bool = True
    clusters: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # task_id -> list of statuses returned on successive polls (None = 404 / no run yet)
    runs: dict[str, list[str | None]] = field(default_factory=dict)
    run_script: list[str | None] = field(default_factory=lambda: [None, "RUNNING", "DONE"])
    down: set[str] = field(default_factory=set)  # Scylla nodes not answering REST
    gossip_down: set[str] = field(default_factory=set)
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    drained: list[str] = field(default_factory=list)

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        host, path = req.url.host, req.url.path
        self.requests.append((req.method, f"{host}{path}", body))
        if host == MANAGER:
            if not self.manager_up:
                raise httpx.ConnectError("refused")
            return self._manager(req.method, path.removeprefix("/api/v1"), body)
        if host in self.down:
            raise httpx.ConnectError("refused")
        return self._scylla(req.method, host, path)

    def _manager(self, method: str, path: str, body: Any) -> httpx.Response:
        parts = path.strip("/").split("/")
        if path == "/version":
            return httpx.Response(200, json={"version": "3.13.0"})
        if path == "/clusters" and method == "POST":
            cid = body.get("id") or "generated-id"
            self.clusters[cid] = body
            return httpx.Response(201, headers={"Location": f"/api/v1/cluster/{cid}"})
        if parts[0] == "cluster" and len(parts) == 2:
            if method == "GET":
                c = self.clusters.get(parts[1])
                return httpx.Response(200, json=c) if c else httpx.Response(404, json={})
            self.clusters[parts[1]] = body
            return httpx.Response(200)
        if parts[0] == "cluster" and parts[2] == "tasks":
            if method == "GET":
                return httpx.Response(200, json=list(self.tasks.values()))
            tid = f"t{len(self.tasks) + 1}"
            self.tasks[tid] = {"id": tid, **body}
            self.runs[tid] = list(self.run_script)
            return httpx.Response(
                201, headers={"Location": f"/api/v1/cluster/{parts[1]}/task/{body['type']}/{tid}"}
            )
        if parts[0] == "cluster" and parts[2] == "task" and len(parts) == 5:  # PUT update
            self.tasks[parts[4]] = {"id": parts[4], **body}
            return httpx.Response(200)
        if parts[0] == "cluster" and parts[2] == "task" and parts[-1] == "latest":
            script = self.runs[parts[4]]
            status = script.pop(0) if len(script) > 1 else script[0]
            if status is None:
                return httpx.Response(404, json={"message": "not found"})
            run = {"status": status, "cause": "boom" if status == "ERROR" else ""}
            return httpx.Response(200, json={"run": run, "progress": {"snapshot_tag": "sm_1"}})
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})

    def _scylla(self, method: str, host: str, path: str) -> httpx.Response:
        if path == "/gossiper/endpoint/live/":
            return httpx.Response(200, json=[ip for ip in DATA_IPS if ip not in self.gossip_down])
        if path == "/gossiper/endpoint/down/":
            return httpx.Response(200, json=sorted(self.gossip_down))
        if path == "/storage_service/nodes/joining":
            return httpx.Response(200, json=[])
        if path == "/storage_service/operation_mode":
            return httpx.Response(200, json="NORMAL")
        if path == "/storage_service/native_transport":
            return httpx.Response(200, json=True)
        if path == "/storage_service/host_id":
            return httpx.Response(200, json=[{"key": ip, "value": f"hid-{ip}"} for ip in DATA_IPS])
        if path == "/storage_service/drain" and method == "POST":
            self.drained.append(host)
            return httpx.Response(200)
        return httpx.Response(404)


async def nolog(_: str) -> None:
    return None


async def nosleep(_: float) -> None:
    return None


class FakeStore:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        self.deleted.append((bucket, prefix))
        return 2


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


def make_ops(world: FakeWorld, **kw: Any) -> ManagerClusterOps:
    args: dict[str, Any] = {
        "auth_token": "tok",
        "transport": httpx.MockTransport(world.handler),
        "sleep": nosleep,
        "poll_interval": 1.0,
    }
    return ManagerClusterOps(**(args | kw))


# --- pure helpers ---------------------------------------------------------------------------


def test_parse_backup_location() -> None:
    loc = parse_backup_location(LOCATION)
    assert (loc.provider, loc.bucket, loc.cluster_id) == ("s3", "dbaas-backups", "c-1234")
    assert loc.manager_location == "s3:dbaas-backups"
    assert parse_backup_location("s3:bucket").cluster_id is None
    with pytest.raises(ValueError):
        parse_backup_location("not a location")


def test_manager_id_is_deterministic_and_prefixes_follow_manager_layout() -> None:
    mid = manager_cluster_uuid("c-1234")
    assert mid == manager_cluster_uuid("c-1234") != manager_cluster_uuid("c-5678")
    assert backup_prefixes(mid) == [
        f"backup/meta/cluster/{mid}/",
        f"backup/sst/cluster/{mid}/",
        f"backup/schema/cluster/{mid}/",
    ]


@pytest.mark.parametrize(
    ("hours", "cron"),
    [(1, "0 * * * *"), (6, "0 */6 * * *"), (24, "0 0 * * *"), (72, "0 0 */3 * *")],
)
def test_cron_for_interval(hours: int, cron: str) -> None:
    assert cron_for_interval(hours) == cron


def test_rest_client_accepts_host_port() -> None:
    assert str(ScyllaRestClient("127.0.0.1:21011")._http.base_url) == "http://127.0.0.1:21011"
    assert str(ScyllaRestClient("10.0.0.1")._http.base_url) == "http://10.0.0.1:10000"


# --- Manager registration and schedules ---------------------------------------------------


async def test_register_creates_then_updates_with_deterministic_id(world: FakeWorld) -> None:
    ops = make_ops(world)
    mid = await ops.register_in_manager(make_infra(), "demo", nolog)
    assert mid == manager_cluster_uuid("c-1234")
    assert world.clusters[mid] == {
        "id": mid,
        "name": "demo",
        "host": DATA_IPS[0],
        "auth_token": "tok",
    }
    assert await ops.register_in_manager(make_infra(), "demo", nolog) == mid
    assert [r[0] for r in world.requests if r[1].endswith(f"/cluster/{mid}")] == [
        "GET",
        "GET",
        "PUT",
    ]


async def test_register_waits_for_manager(world: FakeWorld) -> None:
    world.manager_up = False
    ops = make_ops(world, manager_ready_timeout=3)
    with pytest.raises(ManagerError, match="not reachable"):
        await ops.register_in_manager(make_infra(), "demo", nolog)


async def test_backup_schedule_is_created_once_then_updated(world: FakeWorld) -> None:
    ops = make_ops(world)
    await ops.set_backup_schedule(make_infra(), "m1", 6, 7)
    await ops.set_backup_schedule(make_infra(), "m1", 24, 3)
    assert len(world.tasks) == 1
    task = next(iter(world.tasks.values()))
    assert task["schedule"]["cron"] == "0 0 * * *"
    assert task["properties"] == {"location": ["s3:dbaas-backups"], "retention": 3}


# --- Backup / Restore / Repair --------------------------------------------------------------


async def test_backup_runs_task_and_returns_snapshot(world: FakeWorld) -> None:
    lines: list[str] = []

    async def log(m: str) -> None:
        lines.append(m)

    snap = await make_ops(world).backup(make_infra(), "m1", log)
    assert snap == SnapshotRef("sm_1", LOCATION)
    task = world.tasks["t1"]
    assert task["type"] == "backup"
    assert task["properties"]["location"] == ["s3:dbaas-backups"]
    assert "start_date" in task["schedule"]
    assert any("RUNNING" in line for line in lines)


async def test_backup_failure_raises(world: FakeWorld) -> None:
    world.run_script = ["RUNNING", "ERROR"]
    with pytest.raises(ManagerError, match="ERROR: boom"):
        await make_ops(world).backup(make_infra(), "m1", nolog)


async def test_task_timeout(world: FakeWorld) -> None:
    world.run_script = ["RUNNING"]
    with pytest.raises(ManagerError, match="did not finish"):
        await make_ops(world, task_timeout=3).repair(make_infra(), "m1", nolog)


async def test_restore_schema_then_tables(world: FakeWorld) -> None:
    restarted: list[str] = []

    async def restarter(_: ClusterInfra, node: NodeInfo) -> None:
        restarted.append(node.name)

    snap = SnapshotRef("sm_1", "s3:dbaas-backups/clusters/c-old")
    await make_ops(world, restarter=restarter).restore(make_infra(), "m1", snap, nolog)
    props = [t["properties"] for t in world.tasks.values()]
    base = {"location": ["s3:dbaas-backups"], "snapshot_tag": "sm_1"}
    assert props == [base | {"restore_schema": True}, base | {"restore_tables": True}]
    assert restarted == []  # ScyllaDB >= 2024.2 needs no restart after schema restore

    world.tasks.clear()
    ops = make_ops(world, restarter=restarter, restart_after_schema_restore=True)
    await ops.restore(make_infra(), "m1", snap, nolog)
    assert restarted == ["data-1", "data-2", "data-3"]


# --- health and rolling restart -------------------------------------------------------------


async def test_health_all_up(world: FakeWorld) -> None:
    h = await make_ops(world).health(make_infra())
    assert [(n.name, n.up) for n in h.nodes] == [
        ("data-1", True),
        ("data-2", True),
        ("data-3", True),
    ]
    assert h.monitor_reachable


async def test_health_uses_peer_view_when_node_is_dead(world: FakeWorld) -> None:
    world.down = {DATA_IPS[0]}
    world.gossip_down = {DATA_IPS[0]}
    world.manager_up = False
    h = await make_ops(world).health(make_infra())
    assert [n.up for n in h.nodes] == [False, True, True]
    assert not h.monitor_reachable


async def test_health_nothing_reachable(world: FakeWorld) -> None:
    world.down = set(DATA_IPS)
    h = await make_ops(world).health(make_infra())
    assert [n.up for n in h.nodes] == [False, False, False]


async def test_rolling_restart_drains_and_restarts_one_by_one(world: FakeWorld) -> None:
    events: list[str] = []

    async def restarter(_: ClusterInfra, node: NodeInfo) -> None:
        events.append(f"restart {node.name}")

    async def log(m: str) -> None:
        events.append(m)

    await make_ops(world, restarter=restarter).rolling_restart(make_infra(), log)
    assert world.drained == DATA_IPS
    restarts = [e for e in events if e.startswith("restart ")]
    assert restarts == ["restart data-1", "restart data-2", "restart data-3"]
    assert events.index("restart data-1") < events.index("data-1 is UN again")
    assert events.index("data-1 is UN again") < events.index("draining data-2")


async def test_rolling_restart_requires_restarter(world: FakeWorld) -> None:
    with pytest.raises(NotImplementedError):
        await make_ops(world).rolling_restart(make_infra(), nolog)


async def test_rolling_restart_times_out_if_node_stays_down(world: FakeWorld) -> None:
    async def restarter(_: ClusterInfra, node: NodeInfo) -> None:
        world.gossip_down.add(DATA_IPS[0])

    ops = make_ops(world, restarter=restarter, node_timeout=3)
    with pytest.raises(TimeoutError, match="data-1"):
        await ops.rolling_restart(make_infra(), nolog)


async def test_delete_backups_removes_manager_prefixes(world: FakeWorld) -> None:
    store = FakeStore()
    await make_ops(world, object_store=store).delete_backups(LOCATION, nolog)
    mid = manager_cluster_uuid("c-1234")
    assert store.deleted == [("dbaas-backups", p) for p in backup_prefixes(mid)]
    with pytest.raises(ValueError):
        await make_ops(world, object_store=store).delete_backups("s3:bucket", nolog)


# --- local driver (docker CLI mocked) ---------------------------------------------------------


class FakeDocker:
    def __init__(self, responder: Callable[[tuple[str, ...]], DockerResult]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responder = responder

    async def __call__(
        self, *args: str, stdin: bytes | None = None, check: bool = True
    ) -> DockerResult:
        self.calls.append(args)
        return self.responder(args)


NETWORK_JSON = json.dumps(
    {"IPAM": {"Config": [{"Subnet": "10.213.7.0/24"}]}, "Labels": {"dbaas.agent_token": "t0k"}}
)


def local_responder(args: tuple[str, ...]) -> DockerResult:
    if args[:2] == ("network", "inspect"):
        return DockerResult(0, NETWORK_JSON, "")
    if args[:2] == ("container", "inspect"):
        return DockerResult(1, "", "No such container")
    if args[0] == "ps":
        return DockerResult(
            0,
            "dbaas-c-1-data-1\taaa\trunning\n"
            "dbaas-c-1-data-2\tbbb\texited\n"
            "dbaas-c-1-manager\tmmm\trunning\n"
            "dbaas-c-1-manager-db\tddd\trunning\n"
            "dbaas-c-1-prometheus\tppp\trunning\n"
            "dbaas-c-1-grafana\tggg\trunning",
            "",
        )
    return DockerResult(0, "", "")


async def test_local_describe_maps_containers_and_ports() -> None:
    infra = LocalInfrastructure(LocalSettings(), FakeDocker(local_responder))
    nodes = {n.name: n for n in await infra.describe("c-1")}
    base = 21000 + 20 * 7
    assert nodes["data-1"].state is InstanceState.RUNNING
    assert nodes["data-1"].public_ip == f"127.0.0.1:{base + 1}"
    assert nodes["data-1"].private_ip == "10.213.7.11"
    assert nodes["data-2"].state is InstanceState.STOPPED
    assert nodes["data-3"].state is InstanceState.MISSING
    assert nodes["monitor"].state is InstanceState.RUNNING
    assert nodes["monitor"].private_ip == "10.213.7.21"
    assert rest_endpoint(nodes["data-2"]) == f"127.0.0.1:{base + 12}"


async def test_local_replace_node_uses_host_id_of_dead_node(world: FakeWorld) -> None:
    docker = FakeDocker(local_responder)
    infra = LocalInfrastructure(
        LocalSettings(),
        docker,
        transport=httpx.MockTransport(world.handler),
        sleep=nosleep,
    )
    # The mocked REST maps DATA_IPS; make the network use the same addresses.
    infra_net = json.loads(NETWORK_JSON)
    infra_net["IPAM"]["Config"][0]["Subnet"] = "10.0.1.0/24"

    def responder(args: tuple[str, ...]) -> DockerResult:
        if args[:2] == ("network", "inspect"):
            return DockerResult(0, json.dumps(infra_net), "")
        return local_responder(args)

    docker.responder = responder
    # data-2 is 10.0.1.12 in the driver's layout; teach the fake REST about it.
    DATA_IPS.append("10.0.1.12")
    try:
        await infra.replace_node("c-1", "data-2", nolog)
    finally:
        DATA_IPS.remove("10.0.1.12")

    rm = docker.calls.index(("rm", "-f", "-v", "dbaas-c-1-data-2"))
    create = next(i for i, c in enumerate(docker.calls) if c[0] == "create")
    assert rm < create
    cmd = docker.calls[create]
    assert cmd[cmd.index("--replace-node-first-boot") + 1] == "hid-10.0.1.12"
    assert cmd[cmd.index("--seeds") + 1] == "10.0.1.11,10.0.1.13"
    assert cmd[cmd.index("--ip") + 1] == "10.0.1.12"
    assert "127.0.0.1:21022:9042" in cmd  # slot 1 (3rd octet) -> base 21000 + 20
    assert "127.0.0.1:21032:10000" in cmd
    assert any(c[0] == "cp" and c[-1].endswith("scylla-manager-agent.yaml") for c in docker.calls)
    assert ("start", "dbaas-c-1-data-2") in docker.calls
