"""Health checker and Auto-recovery policy.

Every ``interval`` seconds, for each Cluster that is Healthy or Degraded:

1. ask the Provider for cloud-level state and Scylla for its own view (UN or not);
2. update Node statuses and derive the Cluster status (Degraded if any node is down
   or the Monitor Node is unreachable);
3. if a node has been down longer than ``grace`` and the Cluster has Auto-recovery on,
   submit a Node Replacement (Data Node) or Monitor Node recreation as Actor ``system``,
   pre-empting a running Backup if needed.
"""

import asyncio
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dbaas import audit
from dbaas.domain import (
    SYSTEM_ACTOR,
    AuditResult,
    ClusterStatus,
    NodeRole,
    NodeStatus,
    OperationType,
)
from dbaas.models import Cluster, Node, utcnow
from dbaas.operations.handlers import infra_of
from dbaas.operations.runner import OperationConflict, OperationRunner
from dbaas.providers.base import InstanceState

log = structlog.get_logger("health")

WATCHED = (ClusterStatus.HEALTHY, ClusterStatus.DEGRADED)


class HealthChecker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runner: OperationRunner,
        interval: float,
        grace: float,
    ) -> None:
        self.sessions = sessions
        self.runner = runner
        self.interval = interval
        self.grace = timedelta(seconds=grace)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="health-checker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:
                log.exception("health.check_failed")
            await asyncio.sleep(self.interval)

    async def check_once(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        async with self.sessions() as s:
            clusters = (await s.scalars(select(Cluster).where(Cluster.status.in_(WATCHED)))).all()
            for c in clusters:
                await self._check_cluster(s, c, now)

    async def _check_cluster(self, s: AsyncSession, c: Cluster, now: datetime) -> None:
        provider = self.runner.provider
        cloud = {n.name: n for n in await provider.infra.describe(c.id)}
        scylla = await provider.ops.health(infra_of(c))
        scylla_up = {h.name: h.up for h in scylla.nodes}

        for node in c.nodes:
            info = cloud.get(node.name)
            running = info is not None and info.state is InstanceState.RUNNING
            if node.role is NodeRole.DATA:
                up = running and scylla_up.get(node.name, False)
            else:
                up = running and scylla.monitor_reachable
            self._set_node(s, c, node, up, now)

        any_down = any(n.status is NodeStatus.DOWN for n in c.nodes)
        new_status = ClusterStatus.DEGRADED if any_down else ClusterStatus.HEALTHY
        if new_status != c.status:
            audit.record(
                s,
                actor=SYSTEM_ACTOR,
                action="cluster.status_changed",
                target_type="cluster",
                target_id=c.id,
                result=AuditResult.SUCCEEDED,
                previous=str(c.status),
                current=str(new_status),
            )
            c.status = new_status
        await s.commit()

        if c.auto_recovery:
            await self._maybe_recover(s, c, now)

    def _set_node(self, s: AsyncSession, c: Cluster, node: Node, up: bool, now: datetime) -> None:
        if up and node.status is not NodeStatus.UP:
            node.status, node.down_since = NodeStatus.UP, None
        elif not up and node.status is not NodeStatus.DOWN:
            node.status, node.down_since = NodeStatus.DOWN, now
            audit.record(
                s,
                actor=SYSTEM_ACTOR,
                action="node.down",
                target_type="cluster",
                target_id=c.id,
                result=AuditResult.SUCCEEDED,
                node=node.name,
            )

    async def _maybe_recover(self, s: AsyncSession, c: Cluster, now: datetime) -> None:
        for node in c.nodes:
            if node.status is not NodeStatus.DOWN or node.down_since is None:
                continue
            down_since = node.down_since
            if down_since.tzinfo is None:  # SQLite drops tz info
                down_since = down_since.replace(tzinfo=now.tzinfo)
            if now - down_since < self.grace:
                continue
            if node.role is NodeRole.DATA:
                op_type, params = OperationType.REPLACE_NODE, {"node": node.name}
            else:
                op_type, params = OperationType.RECREATE_MONITOR, {}
            try:
                op = await self.runner.submit(
                    s,
                    cluster_id=c.id,
                    type=op_type,
                    actor=SYSTEM_ACTOR,
                    params={**params, "reason": "auto-recovery"},
                    preempt=True,
                )
                log.info(
                    "auto_recovery.started", cluster_id=c.id, node=node.name, operation_id=op.id
                )
            except OperationConflict as e:
                log.info(
                    "auto_recovery.deferred",
                    cluster_id=c.id,
                    node=node.name,
                    blocking_operation=e.active.id,
                )
            return  # one recovery at a time per Cluster
