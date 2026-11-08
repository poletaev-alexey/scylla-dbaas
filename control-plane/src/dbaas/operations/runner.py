"""Operation runner (ADR-0003).

Every mutating request becomes a persisted Operation executed by an asyncio task inside
the API process. Rules:

* at most one active Operation per Cluster -> ``OperationConflict`` (HTTP 409);
* Auto-recovery may pre-empt a running Backup (``preempt=True``);
* on startup, Operations left active by a previous process are marked failed.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dbaas import audit
from dbaas.domain import (
    PREEMPTIBLE_BY_RECOVERY,
    SYSTEM_ACTOR,
    AuditResult,
    ClusterStatus,
    OperationStatus,
    OperationType,
)
from dbaas.models import Cluster, Operation, OperationLogEntry, utcnow
from dbaas.providers.base import Provider, StepLog

log = structlog.get_logger("operations")

ACTIVE = (OperationStatus.PENDING, OperationStatus.RUNNING)

Handler = Callable[["OperationContext"], Awaitable[None]]


class OperationConflict(Exception):
    def __init__(self, active: Operation) -> None:
        super().__init__(f"Operation {active.id} ({active.type}) is already active on this Cluster")
        self.active = active


class OperationContext:
    def __init__(
        self, session: AsyncSession, op: Operation, provider: Provider, runner: "OperationRunner"
    ) -> None:
        self.session = session
        self.op = op
        self.cluster_id = op.cluster_id  # cached: survives session rollbacks
        self.provider = provider
        self.runner = runner

    @property
    def params(self) -> dict[str, Any]:
        return self.op.params

    async def log(self, message: str) -> None:
        self.session.add(OperationLogEntry(operation_id=self.op.id, message=message))
        await self.session.commit()
        log.info("operation.step", operation_id=self.op.id, type=self.op.type, message=message)

    @property
    def step_log(self) -> StepLog:
        return self.log


class OperationRunner:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: Provider,
        handlers: dict[OperationType, Handler],
    ) -> None:
        self.sessions = sessions
        self.provider = provider
        self.handlers = handlers
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._submit_lock = asyncio.Lock()

    async def recover_after_restart(self) -> None:
        async with self.sessions() as s:
            stale = (await s.scalars(select(Operation).where(Operation.status.in_(ACTIVE)))).all()
            for op in stale:
                op.status = OperationStatus.FAILED
                op.error = "interrupted by Control Plane restart"
                op.finished_at = utcnow()
                audit.record(
                    s,
                    actor=SYSTEM_ACTOR,
                    action=f"{op.type}.interrupted",
                    target_type="operation",
                    target_id=op.id,
                    result=AuditResult.FAILED,
                )
                # The Cluster must not stay in a transitional state nobody will finish:
                # half-built or half-deleted -> Failed (delete is still allowed);
                # interrupted recovery -> Degraded, so the health checker tries again.
                cluster = await s.get(Cluster, op.cluster_id) if op.cluster_id else None
                if cluster is not None:
                    if cluster.status in (ClusterStatus.PROVISIONING, ClusterStatus.DELETING):
                        cluster.status = ClusterStatus.FAILED
                    elif cluster.status is ClusterStatus.RECOVERING:
                        cluster.status = ClusterStatus.DEGRADED
            await s.commit()

    async def submit(
        self,
        session: AsyncSession,
        *,
        cluster_id: str | None,
        type: OperationType,
        actor: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
        preempt: bool = False,
        on_accepted: Callable[[Operation], None] | None = None,
    ) -> Operation:
        async with self._submit_lock:
            if cluster_id is not None:
                active = await session.scalar(
                    select(Operation).where(
                        Operation.cluster_id == cluster_id, Operation.status.in_(ACTIVE)
                    )
                )
                if active is not None:
                    if preempt and active.type in PREEMPTIBLE_BY_RECOVERY:
                        await self._cancel(session, active, reason=f"pre-empted by {type}")
                    else:
                        raise OperationConflict(active)

            op = Operation(cluster_id=cluster_id, type=type, actor=actor, params=params or {})
            session.add(op)
            await session.flush()
            if on_accepted is not None:
                on_accepted(op)
            audit.record(
                session,
                actor=actor,
                action=f"{type}.requested",
                target_type="cluster",
                target_id=cluster_id,
                result=AuditResult.ACCEPTED,
                request_id=request_id,
                operation_id=op.id,
                **(params or {}),
            )
            await session.commit()

        self._tasks[op.id] = asyncio.create_task(self._run(op.id), name=f"op-{op.id}")
        return op

    async def _cancel(self, session: AsyncSession, op: Operation, reason: str) -> None:
        task = self._tasks.get(op.id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await session.execute(
            update(Operation)
            .where(Operation.id == op.id)
            .values(status=OperationStatus.CANCELLED, error=reason, finished_at=utcnow())
        )
        audit.record(
            session,
            actor=SYSTEM_ACTOR,
            action=f"{op.type}.cancelled",
            target_type="cluster",
            target_id=op.cluster_id,
            result=AuditResult.FAILED,
            operation_id=op.id,
            reason=reason,
        )
        await session.flush()

    async def _run(self, op_id: str) -> None:
        async with self.sessions() as s:
            op = await s.get(Operation, op_id)
            assert op is not None
            op.status = OperationStatus.RUNNING
            op.started_at = utcnow()
            await s.commit()
            ctx = OperationContext(s, op, self.provider, self)
            try:
                await self.handlers[op.type](ctx)
            except asyncio.CancelledError:
                await s.rollback()
                await s.refresh(op)
                await ctx.log("cancelled")
                raise
            except Exception as exc:
                await s.rollback()
                await s.refresh(op)  # rollback expired it; async sessions cannot lazy-load
                op.status = OperationStatus.FAILED
                op.error = f"{type(exc).__name__}: {exc}"
                op.finished_at = utcnow()
                audit.record(
                    s,
                    actor=op.actor,
                    action=f"{op.type}.completed",
                    target_type="cluster",
                    target_id=op.cluster_id,
                    result=AuditResult.FAILED,
                    operation_id=op.id,
                    error=op.error,
                )
                await s.commit()
                log.exception("operation.failed", operation_id=op.id)
            else:
                op.status = OperationStatus.SUCCEEDED
                op.finished_at = utcnow()
                audit.record(
                    s,
                    actor=op.actor,
                    action=f"{op.type}.completed",
                    target_type="cluster",
                    target_id=op.cluster_id,
                    result=AuditResult.SUCCEEDED,
                    operation_id=op.id,
                )
                await s.commit()
            finally:
                self._tasks.pop(op_id, None)

    async def wait(self, op_id: str) -> None:
        """Test helper: wait until the Operation's task has finished."""
        task = self._tasks.get(op_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def wait_all(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def shutdown(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
