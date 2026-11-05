"""Provider contract (ADR-0006).

A Provider hides *where* a Cluster runs. Two concerns, two interfaces:

* ``Infrastructure`` creates and destroys VMs/containers and reports their cloud-level state.
  Implemented differently per driver (Terraform on AWS, Docker locally, in-memory fake).
* ``ClusterOps`` talks to ScyllaDB and Scylla Manager running on those nodes: health,
  rolling restart, Backups, Restore. The real implementation (Scylla REST + Manager API)
  is shared by the ``aws`` and ``local`` drivers; the fake driver simulates it.

Every long-running method receives a ``StepLog`` so progress lands in the Operation log.
Implementations must be idempotent where noted: the worker may retry after a crash.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from dbaas.domain import NodeRole

StepLog = Callable[[str], Awaitable[None]]


class InstanceState(StrEnum):
    """Cloud-level state of the VM/container, independent of Scylla."""

    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    TERMINATED = "terminated"
    MISSING = "missing"


@dataclass(frozen=True)
class NodeInfo:
    name: str  # "data-1".."data-3" or "monitor"
    role: NodeRole
    instance_id: str
    state: InstanceState
    public_ip: str | None = None
    private_ip: str | None = None


@dataclass(frozen=True)
class ClusterInfra:
    nodes: list[NodeInfo]
    grafana_url: str
    manager_url: str
    credentials_ref: str  # opaque reference to where the CQL password is stored
    backup_location: str  # e.g. "s3:my-bucket/clusters/c-1234"

    @property
    def data_nodes(self) -> list[NodeInfo]:
        return [n for n in self.nodes if n.role is NodeRole.DATA]

    @property
    def monitor(self) -> NodeInfo:
        return next(n for n in self.nodes if n.role is NodeRole.MONITOR)


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class NodeHealth:
    """Scylla's own view: is the node UN (Up/Normal) according to its peers?"""

    name: str
    up: bool


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_tag: str
    location: str


@dataclass
class ClusterHealth:
    nodes: list[NodeHealth] = field(default_factory=list)
    monitor_reachable: bool = False


class Infrastructure(Protocol):
    async def provision(self, cluster_id: str, name: str, log: StepLog) -> ClusterInfra:
        """Create network, 3 Data Nodes and the Monitor Node; wait until Scylla is up.
        Idempotent: re-running for an existing cluster_id converges to the same result."""
        ...

    async def destroy(self, cluster_id: str, log: StepLog) -> None:
        """Remove every resource of the Cluster except its Backups. Idempotent."""
        ...

    async def replace_node(self, cluster_id: str, node_name: str, log: StepLog) -> NodeInfo:
        """Node Replacement: a new VM takes over the identity of ``node_name``
        (Scylla ``replace_node_first_boot``). Returns the new node."""
        ...

    async def recreate_monitor(self, cluster_id: str, log: StepLog) -> ClusterInfra:
        """Recreate the Monitor Node from scratch. Returns the whole Cluster view because
        Grafana/Manager endpoints change with the Monitor Node and only the driver knows them."""
        ...

    async def describe(self, cluster_id: str) -> list[NodeInfo]:
        """Current cloud-level view of the Cluster's nodes (cheap; called by the health checker)."""
        ...

    async def get_credentials(self, credentials_ref: str) -> Credentials: ...


class ClusterOps(Protocol):
    async def health(self, infra: ClusterInfra) -> ClusterHealth: ...

    async def register_in_manager(self, infra: ClusterInfra, name: str, log: StepLog) -> str:
        """Register the Cluster in Scylla Manager; returns Manager's cluster id."""
        ...

    async def set_backup_schedule(
        self, infra: ClusterInfra, manager_cluster_id: str, interval_hours: int, retention: int
    ) -> None: ...

    async def backup(
        self, infra: ClusterInfra, manager_cluster_id: str, log: StepLog
    ) -> SnapshotRef:
        """Run a Backup now and wait for completion."""
        ...

    async def restore(
        self, infra: ClusterInfra, manager_cluster_id: str, snapshot: SnapshotRef, log: StepLog
    ) -> None:
        """Restore schema, then data, from ``snapshot`` into an empty Cluster (ADR-0005)."""
        ...

    async def repair(self, infra: ClusterInfra, manager_cluster_id: str, log: StepLog) -> None: ...

    async def rolling_restart(self, infra: ClusterInfra, log: StepLog) -> None:
        """Restart Data Nodes one at a time, waiting for each to be UN again."""
        ...

    async def delete_backups(self, location: str, log: StepLog) -> None: ...


@dataclass(frozen=True)
class Provider:
    name: str
    infra: Infrastructure
    ops: ClusterOps
