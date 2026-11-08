"""``ManagerClusterOps``: the real ``ClusterOps`` shared by the ``aws`` and ``local`` drivers.

It talks to ScyllaDB through its REST API (port 10000) and to Scylla Manager through the
Manager REST API (port 5080 on the Monitor Node). Anything driver-specific is injected:

* ``restarter`` - how to restart Scylla on one Data Node (SSM on AWS, ``docker restart``
  locally); the Control Plane never uses SSH (ADR-0002).
* ``auth_token`` - the Manager Agent auth token configured on the Data Nodes.
* ``credentials`` - CQL credentials Manager needs for schema backup/restore.
* ``object_store`` - deletes Backup objects directly in S3/MinIO, because a Cluster's
  Manager is gone once the Cluster is deleted, but its Backups remain (ADR-0005).

Backup location convention (shared with the Infrastructure drivers):
``infra.backup_location == "s3:<bucket>/clusters/<cluster_id>"``. Scylla Manager only
accepts ``<provider>:<bucket>`` (no path prefix, see ``backupspec/location.go``), so the
prefix is dropped for Manager and the Cluster is instead registered in Manager under a
deterministic UUID derived from ``<cluster_id>``. Manager stores every object under
``backup/{meta,sst,schema}/cluster/<manager_cluster_id>/``, which is what
``delete_backups`` removes.
"""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from dbaas.providers.base import (
    ClusterHealth,
    ClusterInfra,
    Credentials,
    NodeHealth,
    NodeInfo,
    SnapshotRef,
    StepLog,
)
from dbaas.scylla.manager import JSON, ManagerClient, ManagerError, TaskRef, utc_now_iso
from dbaas.scylla.rest import ScyllaRestClient

# Fixed namespace so the same Control Plane cluster id always maps to the same Manager id.
MANAGER_ID_NAMESPACE = uuid.UUID("4f1c2a1e-6d0b-4d55-9a3e-1b7f3c9e2d10")
BACKUP_SCHEDULE_TASK = "dbaas-backup-schedule"
MANAGER_PORT = 5080
REGISTER_ATTEMPTS = 24  # x poll_interval (5 s by default)

NodeRestarter = Callable[[ClusterInfra, NodeInfo], Awaitable[None]]
TokenResolver = Callable[[ClusterInfra], Awaitable[str]]
CredentialsResolver = Callable[[ClusterInfra], Awaitable[Credentials | None]]
Sleep = Callable[[float], Awaitable[None]]


class ObjectStore(Protocol):
    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        """Delete every object under ``prefix``; return how many were deleted."""
        ...


@dataclass(frozen=True)
class BackupLocation:
    provider: str  # "s3"
    bucket: str
    cluster_id: str | None

    @property
    def manager_location(self) -> str:
        return f"{self.provider}:{self.bucket}"


_LOCATION = re.compile(r"^(?P<provider>[a-z0-9]+):(?P<bucket>[a-z0-9.\-]+)(?P<path>/.*)?$")


def parse_backup_location(location: str) -> BackupLocation:
    m = _LOCATION.match(location)
    if m is None:
        raise ValueError(f"unsupported backup location {location!r}")
    path = (m.group("path") or "").strip("/")
    cluster_id = path.rsplit("/", 1)[-1] if path else None
    return BackupLocation(m.group("provider"), m.group("bucket"), cluster_id)


def manager_cluster_uuid(cluster_id: str) -> str:
    return str(uuid.uuid5(MANAGER_ID_NAMESPACE, cluster_id))


def backup_prefixes(manager_cluster_id: str) -> list[str]:
    return [f"backup/{kind}/cluster/{manager_cluster_id}/" for kind in ("meta", "sst", "schema")]


def cron_for_interval(hours: int) -> str:
    """Manager 3.x schedules use cron (``interval`` is deprecated in the swagger spec).
    Intervals that do not divide a day are approximated (the cron resets at midnight)."""
    if hours < 1:
        raise ValueError("backup interval must be at least 1 hour")
    if hours < 24:
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    days = hours // 24
    return "0 0 * * *" if days == 1 else f"0 0 */{days} * *"


def static_token(token: str) -> TokenResolver:
    async def resolve(_: ClusterInfra) -> str:
        return token

    return resolve


async def _no_credentials(_: ClusterInfra) -> Credentials | None:
    return None


def default_manager_url(infra: ClusterInfra) -> str:
    return infra.manager_url


def default_node_address(node: NodeInfo) -> str | None:
    return node.public_ip or node.private_ip


class ManagerClusterOps:
    def __init__(
        self,
        *,
        auth_token: str | TokenResolver = "",
        restarter: NodeRestarter | None = None,
        credentials: CredentialsResolver = _no_credentials,
        object_store: ObjectStore | None = None,
        manager_url: Callable[[ClusterInfra], str] = default_manager_url,
        node_address: Callable[[NodeInfo], str | None] = default_node_address,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval: float = 5.0,
        task_timeout: float = 6 * 3600.0,
        node_timeout: float = 600.0,
        manager_ready_timeout: float = 600.0,
        restart_after_schema_restore: bool = False,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._token = static_token(auth_token) if isinstance(auth_token, str) else auth_token
        self._restarter = restarter
        self._credentials = credentials
        self._store = object_store
        self._manager_url = manager_url
        self._node_address = node_address
        self._transport = transport
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.node_timeout = node_timeout
        self.manager_ready_timeout = manager_ready_timeout
        # Manager docs: a rolling restart after restoring schema is needed only for
        # ScyllaDB 5.4/2024.1 or older (schema restored via sstables). Newer versions restore
        # schema over CQL (DESCRIBE SCHEMA WITH INTERNALS) and need no restart.
        self.restart_after_schema_restore = restart_after_schema_restore
        self._sleep = sleep

    # --- clients --------------------------------------------------------------------------

    def _manager(self, infra: ClusterInfra) -> ManagerClient:
        return ManagerClient(self._manager_url(infra), transport=self._transport)

    def _rest(self, address: str) -> ScyllaRestClient:
        return ScyllaRestClient(address, transport=self._transport)

    def _gossip_address(self, node: NodeInfo) -> str | None:
        """The address peers know a node by (its listen address = private IP)."""
        return node.private_ip or node.public_ip

    # --- health ---------------------------------------------------------------------------

    async def _ring_view(self, infra: ClusterInfra) -> tuple[set[str], set[str]] | None:
        """(live, not-normal) gossip view from the first Data Node that answers."""
        for node in infra.data_nodes:
            addr = self._node_address(node)
            if not addr:
                continue
            try:
                async with self._rest(addr) as rest:
                    live = await rest.live_endpoints()
                    down = await rest.down_endpoints()
                    joining = await rest.joining_nodes()
            except (httpx.HTTPError, RuntimeError, ValueError):
                continue
            return live - down, joining | down
        return None

    async def _node_is_un(self, infra: ClusterInfra, node: NodeInfo) -> bool:
        view = await self._ring_view(infra)
        gossip = self._gossip_address(node)
        if view is None or gossip is None:
            return False
        live, not_normal = view
        if gossip not in live or gossip in not_normal:
            return False
        addr = self._node_address(node)
        if not addr:
            return False
        try:
            async with self._rest(addr) as rest:
                return (
                    await rest.operation_mode() == "NORMAL"
                    and await rest.native_transport_running()
                )
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    async def health(self, infra: ClusterInfra) -> ClusterHealth:
        view = await self._ring_view(infra)
        nodes: list[NodeHealth] = []
        for n in infra.data_nodes:
            gossip = self._gossip_address(n)
            up = (
                view is not None
                and gossip is not None
                and gossip in view[0]
                and gossip not in view[1]
            )
            nodes.append(NodeHealth(n.name, up))
        return ClusterHealth(nodes=nodes, monitor_reachable=await self._manager_reachable(infra))

    async def _manager_reachable(self, infra: ClusterInfra) -> bool:
        if not self._manager_url(infra):
            return False
        try:
            async with self._manager(infra) as mgr:
                await mgr.version()
            return True
        except (httpx.HTTPError, ManagerError):
            return False

    # --- Manager registration and schedules ----------------------------------------------

    async def _wait_manager_ready(self, infra: ClusterInfra, log: StepLog) -> None:
        waited = 0.0
        while not await self._manager_reachable(infra):
            if waited >= self.manager_ready_timeout:
                raise ManagerError(f"Scylla Manager at {self._manager_url(infra)} not reachable")
            if waited == 0:
                await log("waiting for Scylla Manager API")
            await self._sleep(self.poll_interval)
            waited += self.poll_interval

    async def register_in_manager(self, infra: ClusterInfra, name: str, log: StepLog) -> str:
        await self._wait_manager_ready(infra, log)
        loc = parse_backup_location(infra.backup_location)
        host = next((a for n in infra.data_nodes if (a := self._gossip_address(n))), None)
        if host is None:
            raise ValueError("Cluster has no Data Node address to register in Manager")
        body: JSON = {"name": name, "host": host, "auth_token": await self._token(infra)}
        creds = await self._credentials(infra)
        if creds is not None:
            body |= {"username": creds.username, "password": creds.password}

        # Manager checks every node's Agent on :10001 before accepting the Cluster. A node
        # serves CQL a few seconds before its Agent starts (seen on AWS), so retry that case.
        attempts = REGISTER_ATTEMPTS
        while True:
            try:
                cid = await self._add_or_update_cluster(infra, loc.cluster_id, body)
                break
            except ManagerError as e:
                attempts -= 1
                if "connectivity check" not in str(e) or attempts <= 0:
                    raise
                await log("Scylla Manager cannot reach every Agent yet, retrying")
                await self._sleep(self.poll_interval)
        await log(f"registered {name} in Scylla Manager as {cid} (host {host})")
        return cid

    async def _add_or_update_cluster(
        self, infra: ClusterInfra, cluster_id: str | None, body: JSON
    ) -> str:
        async with self._manager(infra) as mgr:
            if cluster_id is None:
                return await mgr.add_cluster(body)
            cid = manager_cluster_uuid(cluster_id)
            if await mgr.get_cluster(cid) is None:
                await mgr.add_cluster({"id": cid, **body})
            else:  # idempotent re-run / Monitor Node recreated with the same Manager DB
                await mgr.update_cluster(cid, {"id": cid, **body})
            return cid

    async def set_backup_schedule(
        self, infra: ClusterInfra, manager_cluster_id: str, interval_hours: int, retention: int
    ) -> None:
        loc = parse_backup_location(infra.backup_location)
        body: JSON = {
            "type": "backup",
            "name": BACKUP_SCHEDULE_TASK,
            "enabled": True,
            "schedule": {"cron": cron_for_interval(interval_hours), "num_retries": 3},
            "properties": {"location": [loc.manager_location], "retention": retention},
        }
        async with self._manager(infra) as mgr:
            existing = next(
                (
                    t
                    for t in await mgr.list_tasks(manager_cluster_id, "backup")
                    if t.get("name") == BACKUP_SCHEDULE_TASK
                ),
                None,
            )
            if existing is None:
                await mgr.create_task(
                    manager_cluster_id,
                    "backup",
                    name=BACKUP_SCHEDULE_TASK,
                    properties=body["properties"],
                    schedule=body["schedule"],
                )
            else:
                ref = TaskRef(manager_cluster_id, "backup", str(existing["id"]))
                await mgr.update_task(ref, body)

    # --- one-off Manager tasks -------------------------------------------------------------

    async def _run_task(
        self,
        infra: ClusterInfra,
        manager_cluster_id: str,
        task_type: str,
        properties: JSON,
        log: StepLog,
    ) -> JSON:
        """Create a one-shot task that starts now, wait for it, return its last progress."""
        name = f"dbaas-{task_type}-{utc_now_iso().replace(':', '')}"
        async with self._manager(infra) as mgr:
            ref = await mgr.create_task(
                manager_cluster_id,
                task_type,
                name=name,
                properties=properties,
                # No cron/interval/start_date: the task never fires on its own. It is started
                # explicitly below. Manager 3.13 silently drops a one-shot start_date that is
                # already in the past when the scheduler sees it ("no activations scheduled"),
                # so start_date=now left Backups stuck in NEW (found by the local e2e test).
                schedule={"num_retries": 0},
            )
            await mgr.start_task(ref)
            await log(f"Manager {task_type} task {ref.task_id} started")

            async def on_progress(data: JSON) -> None:
                await log(f"Manager {task_type} task: {data['run'].get('status')}")

            return await mgr.wait_for_run(
                ref,
                max_wait=self.task_timeout,
                poll_interval=self.poll_interval,
                sleep=self._sleep,
                on_progress=on_progress,
            )

    async def backup(
        self, infra: ClusterInfra, manager_cluster_id: str, log: StepLog
    ) -> SnapshotRef:
        loc = parse_backup_location(infra.backup_location)
        data = await self._run_task(
            infra,
            manager_cluster_id,
            "backup",
            # Each ad-hoc Backup is its own task, so retention=1 keeps exactly its snapshot.
            {"location": [loc.manager_location], "retention": 1},
            log,
        )
        progress: dict[str, Any] = data.get("progress") or {}
        tag = progress.get("snapshot_tag")
        if not tag:
            raise ManagerError("backup finished but Manager reported no snapshot tag")
        await log(f"snapshot {tag} uploaded to {loc.manager_location}")
        return SnapshotRef(snapshot_tag=str(tag), location=infra.backup_location)

    async def list_snapshots(
        self, infra: ClusterInfra, manager_cluster_id: str
    ) -> list[SnapshotRef]:
        loc = parse_backup_location(infra.backup_location)
        async with self._manager(infra) as mgr:
            items = await mgr.list_backups(manager_cluster_id, [loc.manager_location])
        tags = {
            str(s["snapshot_tag"])
            for item in items
            for s in (item.get("snapshot_info") or [])
            if s.get("snapshot_tag")
        }
        return [SnapshotRef(snapshot_tag=t, location=infra.backup_location) for t in sorted(tags)]

    async def restore(
        self, infra: ClusterInfra, manager_cluster_id: str, snapshot: SnapshotRef, log: StepLog
    ) -> None:
        # Manager scans all cluster ids in the location for the snapshot tag
        # (restore/worker_manifest.go), so a Backup of another Cluster can be restored here.
        loc = parse_backup_location(snapshot.location).manager_location
        base: JSON = {"location": [loc], "snapshot_tag": snapshot.snapshot_tag}
        await log(f"restoring schema from {snapshot.snapshot_tag}")
        await self._run_task(
            infra, manager_cluster_id, "restore", base | {"restore_schema": True}, log
        )
        if self.restart_after_schema_restore:
            await log("rolling restart so every Data Node picks up the restored schema")
            await self.rolling_restart(infra, log)
        await log("restoring table data")
        await self._run_task(
            infra, manager_cluster_id, "restore", base | {"restore_tables": True}, log
        )

    async def repair(self, infra: ClusterInfra, manager_cluster_id: str, log: StepLog) -> None:
        await self._run_task(infra, manager_cluster_id, "repair", {}, log)
        await log("repair finished")

    # --- rolling restart -------------------------------------------------------------------

    async def wait_node_un(self, infra: ClusterInfra, node: NodeInfo) -> None:
        waited = 0.0
        while not await self._node_is_un(infra, node):
            if waited >= self.node_timeout:
                raise TimeoutError(f"{node.name} did not become UN within {self.node_timeout}s")
            await self._sleep(self.poll_interval)
            waited += self.poll_interval

    async def rolling_restart(self, infra: ClusterInfra, log: StepLog) -> None:
        if self._restarter is None:
            raise NotImplementedError("no restart mechanism configured for this Provider")
        for node in infra.data_nodes:
            addr = self._node_address(node)
            await log(f"draining {node.name}")
            if addr:
                try:
                    async with self._rest(addr) as rest:
                        await rest.drain()
                except (httpx.HTTPError, RuntimeError) as e:
                    await log(f"drain of {node.name} failed ({e}); restarting anyway")
            await log(f"restarting {node.name}")
            await self._restarter(infra, node)
            await self.wait_node_un(infra, node)
            await log(f"{node.name} is UN again")

    # --- Backup deletion -------------------------------------------------------------------

    async def delete_backups(self, location: str, log: StepLog) -> None:
        loc = parse_backup_location(location)
        if loc.cluster_id is None:
            raise ValueError(f"cannot tell which Cluster owns {location!r}")
        if self._store is None:
            raise NotImplementedError("no object store configured for Backup deletion")
        mid = manager_cluster_uuid(loc.cluster_id)
        total = 0
        for prefix in backup_prefixes(mid):
            total += await self._store.delete_prefix(loc.bucket, prefix)
        await log(f"deleted {total} objects of Manager cluster {mid} from {loc.manager_location}")
