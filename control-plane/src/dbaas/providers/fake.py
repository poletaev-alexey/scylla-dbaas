"""In-memory Provider for unit tests and UI development without Docker or AWS.

Supports failure injection (``kill_node``, ``kill_monitor``) so Auto-recovery can be
exercised deterministically.
"""

import asyncio
import secrets
from dataclasses import dataclass, field, replace

from dbaas.domain import DATA_NODE_COUNT, NodeRole
from dbaas.providers.base import (
    ClusterHealth,
    ClusterInfra,
    Credentials,
    InstanceState,
    NodeHealth,
    NodeInfo,
    Provider,
    SnapshotRef,
    StepLog,
)


@dataclass
class _FakeCluster:
    name: str
    nodes: dict[str, NodeInfo]
    password: str
    tables: dict[str, list[str]] = field(default_factory=dict)


class FakeWorld:
    """Shared state between FakeInfrastructure and FakeClusterOps."""

    def __init__(self, step_delay: float = 0.0) -> None:
        self.step_delay = step_delay
        self.clusters: dict[str, _FakeCluster] = {}
        self.snapshots: dict[str, dict[str, list[str]]] = {}  # location/tag -> data
        self.fail_next: set[str] = set()  # method names that raise once

    async def step(self, log: StepLog, msg: str) -> None:
        await log(msg)
        await asyncio.sleep(self.step_delay)

    def maybe_fail(self, method: str) -> None:
        if method in self.fail_next:
            self.fail_next.discard(method)
            raise RuntimeError(f"injected failure in {method}")

    def kill_node(self, cluster_id: str, node_name: str) -> None:
        c = self.clusters[cluster_id]
        c.nodes[node_name] = replace(c.nodes[node_name], state=InstanceState.TERMINATED)

    def kill_monitor(self, cluster_id: str) -> None:
        self.kill_node(cluster_id, "monitor")


def _node(name: str, role: NodeRole, n: int) -> NodeInfo:
    return NodeInfo(
        name=name,
        role=role,
        instance_id=f"i-{secrets.token_hex(6)}",
        state=InstanceState.RUNNING,
        public_ip=f"203.0.113.{n}",
        private_ip=f"10.0.1.{n}",
    )


class FakeInfrastructure:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def _infra(self, cluster_id: str) -> ClusterInfra:
        c = self.w.clusters[cluster_id]
        mon = c.nodes["monitor"]
        return ClusterInfra(
            nodes=list(c.nodes.values()),
            grafana_url=f"http://{mon.public_ip}:3000",
            manager_url=f"http://{mon.public_ip}:5080",
            credentials_ref=f"fake:{cluster_id}",
            backup_location=f"s3:fake-bucket/clusters/{cluster_id}",
        )

    async def provision(self, cluster_id: str, name: str, log: StepLog) -> ClusterInfra:
        self.w.maybe_fail("provision")
        if cluster_id not in self.w.clusters:
            await self.w.step(log, "creating network and security groups")
            nodes = {
                f"data-{i}": _node(f"data-{i}", NodeRole.DATA, i)
                for i in range(1, DATA_NODE_COUNT + 1)
            }
            await self.w.step(log, "launching 3 Data Nodes")
            nodes["monitor"] = _node("monitor", NodeRole.MONITOR, 10)
            await self.w.step(log, "launching Monitor Node")
            self.w.clusters[cluster_id] = _FakeCluster(
                name=name, nodes=nodes, password=secrets.token_urlsafe(12)
            )
            await self.w.step(log, "waiting for ScyllaDB to report UN on all Data Nodes")
        return self._infra(cluster_id)

    async def destroy(self, cluster_id: str, log: StepLog) -> None:
        self.w.maybe_fail("destroy")
        await self.w.step(log, "destroying VMs and network")
        self.w.clusters.pop(cluster_id, None)

    async def replace_node(self, cluster_id: str, node_name: str, log: StepLog) -> NodeInfo:
        self.w.maybe_fail("replace_node")
        c = self.w.clusters[cluster_id]
        old = c.nodes[node_name]
        await self.w.step(log, f"launching replacement VM for {node_name} ({old.private_ip})")
        new = replace(old, instance_id=f"i-{secrets.token_hex(6)}", state=InstanceState.RUNNING)
        c.nodes[node_name] = new
        await self.w.step(log, f"{node_name} joined with replace_node_first_boot")
        return new

    async def recreate_monitor(self, cluster_id: str, log: StepLog) -> ClusterInfra:
        await self.replace_node(cluster_id, "monitor", log)
        return self._infra(cluster_id)

    async def describe(self, cluster_id: str) -> list[NodeInfo]:
        c = self.w.clusters.get(cluster_id)
        return list(c.nodes.values()) if c else []

    async def get_credentials(self, credentials_ref: str) -> Credentials:
        cluster_id = credentials_ref.removeprefix("fake:")
        return Credentials("scylla", self.w.clusters[cluster_id].password)


class FakeClusterOps:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def _cluster_id(self, infra: ClusterInfra) -> str:
        return infra.credentials_ref.removeprefix("fake:")

    async def health(self, infra: ClusterInfra) -> ClusterHealth:
        c = self.w.clusters.get(self._cluster_id(infra))
        if c is None:
            return ClusterHealth()
        return ClusterHealth(
            nodes=[
                NodeHealth(n.name, n.state is InstanceState.RUNNING)
                for n in c.nodes.values()
                if n.role is NodeRole.DATA
            ],
            monitor_reachable=c.nodes["monitor"].state is InstanceState.RUNNING,
        )

    async def register_in_manager(self, infra: ClusterInfra, name: str, log: StepLog) -> str:
        await self.w.step(log, f"registering {name} in Scylla Manager")
        return f"mgr-{secrets.token_hex(4)}"

    async def set_backup_schedule(
        self, infra: ClusterInfra, manager_cluster_id: str, interval_hours: int, retention: int
    ) -> None:
        return None

    async def backup(
        self, infra: ClusterInfra, manager_cluster_id: str, log: StepLog
    ) -> SnapshotRef:
        self.w.maybe_fail("backup")
        await self.w.step(log, f"Manager backup task started → {infra.backup_location}")
        tag = f"sm_{secrets.token_hex(6)}"
        c = self.w.clusters[self._cluster_id(infra)]
        self.w.snapshots[f"{infra.backup_location}/{tag}"] = {
            k: list(v) for k, v in c.tables.items()
        }
        await self.w.step(log, f"snapshot {tag} uploaded")
        return SnapshotRef(snapshot_tag=tag, location=infra.backup_location)

    async def restore(
        self, infra: ClusterInfra, manager_cluster_id: str, snapshot: SnapshotRef, log: StepLog
    ) -> None:
        self.w.maybe_fail("restore")
        await self.w.step(log, f"restoring schema from {snapshot.snapshot_tag}")
        data = self.w.snapshots[f"{snapshot.location}/{snapshot.snapshot_tag}"]
        await self.w.step(log, "restoring table data")
        self.w.clusters[self._cluster_id(infra)].tables = {k: list(v) for k, v in data.items()}

    async def repair(self, infra: ClusterInfra, manager_cluster_id: str, log: StepLog) -> None:
        await self.w.step(log, "repair finished")

    async def rolling_restart(self, infra: ClusterInfra, log: StepLog) -> None:
        for n in infra.data_nodes:
            await self.w.step(log, f"restarting {n.name}, waiting for UN")

    async def delete_backups(self, location: str, log: StepLog) -> None:
        for key in [k for k in self.w.snapshots if k.startswith(location)]:
            del self.w.snapshots[key]
        await self.w.step(log, f"deleted backups under {location}")


def fake_provider(world: FakeWorld | None = None) -> tuple[Provider, FakeWorld]:
    w = world or FakeWorld()
    return Provider("fake", FakeInfrastructure(w), FakeClusterOps(w)), w
