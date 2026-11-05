from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select

from dbaas import audit
from dbaas.api.deps import Actor, Cfg, RequestId, Runner, Session
from dbaas.domain import (
    AuditResult,
    BackupStatus,
    ClusterStatus,
    NodeRole,
    OperationType,
)
from dbaas.models import AuditEvent, Backup, Cluster, Operation
from dbaas.operations.runner import OperationConflict
from dbaas.schemas import (
    Accepted,
    AuditEventOut,
    BackupOut,
    BackupScheduleIn,
    BackupScheduleOut,
    ClusterCreate,
    ClusterOut,
    ClusterPatch,
    ConnectionInfo,
    OperationDetail,
    OperationOut,
    Problem,
)

router = APIRouter(prefix="/api/v1")

CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"model": Problem, "description": "another Operation is active"}
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": Problem}}
GONE = (ClusterStatus.DELETING, ClusterStatus.DELETED)


async def _get_cluster(s: Session, cluster_id: str) -> Cluster:
    c = await s.get(Cluster, cluster_id)
    if c is None or c.status is ClusterStatus.DELETED:
        raise HTTPException(404, f"Cluster {cluster_id} not found")
    return c


async def _submit(
    s: Session,
    runner: Runner,
    *,
    cluster: Cluster | None,
    cluster_id: str | None,
    type: OperationType,
    actor: str,
    rid: str,
    params: dict[str, Any] | None = None,
) -> Accepted:
    try:
        op = await runner.submit(
            s, cluster_id=cluster_id, type=type, actor=actor, params=params, request_id=rid
        )
    except OperationConflict as e:
        audit.record(
            s,
            actor=actor,
            action=f"{type}.requested",
            target_type="cluster",
            target_id=cluster_id,
            result=AuditResult.DENIED,
            request_id=rid,
            reason="conflict",
            active_operation_id=e.active.id,
        )
        await s.commit()
        raise HTTPException(409, detail=str(e)) from e
    if cluster is not None:
        await s.refresh(cluster)
    return Accepted(
        operation=OperationOut.model_validate(op),
        cluster=ClusterOut.model_validate(cluster) if cluster else None,
    )


# ---- Clusters ---------------------------------------------------------------------------


@router.post("/clusters", status_code=202, response_model=Accepted, responses=CONFLICT)
async def create_cluster(
    body: ClusterCreate,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
    cfg: Cfg,
) -> Accepted:
    taken = await s.scalar(
        select(Cluster).where(Cluster.name == body.name, Cluster.status.not_in(GONE))
    )
    if taken is not None:
        raise HTTPException(409, f"Cluster name {body.name!r} is already in use")
    if body.from_backup_id is not None:
        b = await s.get(Backup, body.from_backup_id)
        if b is None or b.status is not BackupStatus.DONE:
            raise HTTPException(422, f"Backup {body.from_backup_id} is not restorable")

    c = Cluster(
        name=body.name,
        auto_recovery=body.auto_recovery,
        source_backup_id=body.from_backup_id,
        backup_interval_hours=cfg.default_backup_interval_hours,
        backup_retention=cfg.default_backup_retention,
    )
    s.add(c)
    await s.flush()
    return await _submit(
        s,
        runner,
        cluster=c,
        cluster_id=c.id,
        type=OperationType.CREATE_CLUSTER,
        actor=actor,
        rid=rid,
        params={"name": body.name, "from_backup_id": body.from_backup_id},
    )


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(
    s: Session,
    _: Actor,
    include_deleted: bool = False,
) -> list[Cluster]:
    q = select(Cluster).order_by(Cluster.created_at.desc())
    if not include_deleted:
        q = q.where(Cluster.status != ClusterStatus.DELETED)
    return list((await s.scalars(q)).all())


@router.get("/clusters/{cluster_id}", response_model=ClusterOut, responses=NOT_FOUND)
async def get_cluster(cluster_id: str, s: Session, _: Actor) -> Cluster:
    return await _get_cluster(s, cluster_id)


@router.patch("/clusters/{cluster_id}", response_model=ClusterOut, responses=NOT_FOUND)
async def patch_cluster(
    cluster_id: str,
    body: ClusterPatch,
    s: Session,
    actor: Actor,
    rid: RequestId,
) -> Cluster:
    c = await _get_cluster(s, cluster_id)
    if body.auto_recovery is not None:
        c.auto_recovery = body.auto_recovery
        audit.record(
            s,
            actor=actor,
            action="cluster.auto_recovery_changed",
            target_type="cluster",
            target_id=c.id,
            result=AuditResult.SUCCEEDED,
            request_id=rid,
            enabled=body.auto_recovery,
        )
    await s.commit()
    await s.refresh(c)
    return c


@router.delete(
    "/clusters/{cluster_id}",
    status_code=202,
    response_model=Accepted,
    responses={**CONFLICT, **NOT_FOUND},
)
async def delete_cluster(
    cluster_id: str,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
    confirm: str = Query(description="Must equal the Cluster name"),
) -> Accepted:
    c = await _get_cluster(s, cluster_id)
    if confirm != c.name:
        raise HTTPException(422, "confirm must equal the Cluster name")
    return await _submit(
        s,
        runner,
        cluster=c,
        cluster_id=c.id,
        type=OperationType.DELETE_CLUSTER,
        actor=actor,
        rid=rid,
    )


@router.post(
    "/clusters/{cluster_id}/nodes/{node}/replace",
    status_code=202,
    response_model=Accepted,
    responses={**CONFLICT, **NOT_FOUND},
)
async def replace_node(
    cluster_id: str,
    node: str,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
) -> Accepted:
    c = await _get_cluster(s, cluster_id)
    n = next((x for x in c.nodes if x.name == node), None)
    if n is None:
        raise HTTPException(404, f"node {node} not found")
    op_type = (
        OperationType.REPLACE_NODE if n.role is NodeRole.DATA else OperationType.RECREATE_MONITOR
    )
    return await _submit(
        s,
        runner,
        cluster=c,
        cluster_id=c.id,
        type=op_type,
        actor=actor,
        rid=rid,
        params={"node": node},
    )


@router.post(
    "/clusters/{cluster_id}/rolling-restart",
    status_code=202,
    response_model=Accepted,
    responses={**CONFLICT, **NOT_FOUND},
)
async def rolling_restart(
    cluster_id: str,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
) -> Accepted:
    c = await _get_cluster(s, cluster_id)
    return await _submit(
        s,
        runner,
        cluster=c,
        cluster_id=c.id,
        type=OperationType.ROLLING_RESTART,
        actor=actor,
        rid=rid,
    )


@router.get("/clusters/{cluster_id}/connection", response_model=ConnectionInfo)
async def connection(
    cluster_id: str,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
) -> ConnectionInfo:
    c = await _get_cluster(s, cluster_id)
    if c.credentials_ref is None:
        raise HTTPException(409, "Cluster is not provisioned yet")
    creds = await runner.provider.infra.get_credentials(c.credentials_ref)
    points = [n.public_ip for n in c.nodes if n.role is NodeRole.DATA and n.public_ip]
    password = None if c.password_revealed else creds.password
    if password is not None:
        c.password_revealed = True
    audit.record(
        s,
        actor=actor,
        action="cluster.connection_viewed",
        target_type="cluster",
        target_id=c.id,
        result=AuditResult.SUCCEEDED,
        request_id=rid,
        password_revealed=password is not None,
    )
    await s.commit()
    return ConnectionInfo(
        contact_points=points,
        username=creds.username,
        password=password,
        cql_client_command=(
            f"cql-client --hosts {','.join(points)} "
            f"--username {creds.username} --password '<password>' demo"
        ),
    )


# ---- Backups ----------------------------------------------------------------------------


@router.get("/clusters/{cluster_id}/backup-schedule", response_model=BackupScheduleOut)
async def get_schedule(cluster_id: str, s: Session, _: Actor) -> BackupScheduleOut:
    c = await _get_cluster(s, cluster_id)
    return BackupScheduleOut(interval_hours=c.backup_interval_hours, retention=c.backup_retention)


@router.put("/clusters/{cluster_id}/backup-schedule", response_model=BackupScheduleOut)
async def put_schedule(
    cluster_id: str,
    body: BackupScheduleIn,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
) -> BackupScheduleOut:
    from dbaas.operations.handlers import infra_of

    c = await _get_cluster(s, cluster_id)
    if c.manager_cluster_id:
        await runner.provider.ops.set_backup_schedule(
            infra_of(c), c.manager_cluster_id, body.interval_hours, body.retention
        )
    c.backup_interval_hours, c.backup_retention = body.interval_hours, body.retention
    audit.record(
        s,
        actor=actor,
        action="backup_schedule.updated",
        target_type="cluster",
        target_id=c.id,
        result=AuditResult.SUCCEEDED,
        request_id=rid,
        **body.model_dump(),
    )
    await s.commit()
    return BackupScheduleOut(**body.model_dump())


@router.post(
    "/clusters/{cluster_id}/backups",
    status_code=202,
    response_model=Accepted,
    responses={**CONFLICT, **NOT_FOUND},
)
async def backup_now(
    cluster_id: str,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
) -> Accepted:
    c = await _get_cluster(s, cluster_id)
    return await _submit(
        s, runner, cluster=c, cluster_id=c.id, type=OperationType.BACKUP, actor=actor, rid=rid
    )


@router.get("/clusters/{cluster_id}/backups", response_model=list[BackupOut])
async def list_cluster_backups(cluster_id: str, s: Session, _: Actor) -> list[Backup]:
    q = select(Backup).where(Backup.cluster_id == cluster_id).order_by(Backup.created_at.desc())
    return list((await s.scalars(q)).all())


@router.delete("/clusters/{cluster_id}/backups", status_code=202, response_model=Accepted)
async def delete_cluster_backups(
    cluster_id: str,
    s: Session,
    actor: Actor,
    runner: Runner,
    rid: RequestId,
) -> Accepted:
    b = await s.scalar(select(Backup).where(Backup.cluster_id == cluster_id))
    if b is None:
        raise HTTPException(404, "no Backups for this Cluster")
    location = b.location  # all Backups of a Cluster share its prefix
    return await _submit(
        s,
        runner,
        cluster=None,
        cluster_id=cluster_id,
        type=OperationType.DELETE_BACKUPS,
        actor=actor,
        rid=rid,
        params={"cluster_id": cluster_id, "location": location},
    )


@router.get("/backups", response_model=list[BackupOut])
async def list_backups(s: Session, _: Actor) -> list[Backup]:
    q = select(Backup).where(Backup.status != BackupStatus.DELETED)
    return list((await s.scalars(q.order_by(Backup.created_at.desc()))).all())


@router.get("/backups/{backup_id}", response_model=BackupOut, responses=NOT_FOUND)
async def get_backup(backup_id: str, s: Session, _: Actor) -> Backup:
    b = await s.get(Backup, backup_id)
    if b is None:
        raise HTTPException(404, f"Backup {backup_id} not found")
    return b


# ---- Operations & audit -----------------------------------------------------------------


@router.get("/operations", response_model=list[OperationOut])
async def list_operations(
    s: Session,
    _: Actor,
    cluster_id: str | None = None,
    limit: int = Query(50, le=500),
) -> list[Operation]:
    q = select(Operation).order_by(Operation.created_at.desc()).limit(limit)
    if cluster_id:
        q = q.where(Operation.cluster_id == cluster_id)
    return list((await s.scalars(q)).all())


@router.get("/operations/{operation_id}", response_model=OperationDetail, responses=NOT_FOUND)
async def get_operation(operation_id: str, s: Session, _: Actor) -> Operation:
    op = await s.get(Operation, operation_id)
    if op is None:
        raise HTTPException(404, f"Operation {operation_id} not found")
    return op


@router.get("/audit-events", response_model=list[AuditEventOut])
async def list_audit_events(
    s: Session,
    _: Actor,
    cluster_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = Query(100, le=1000),
) -> list[AuditEvent]:
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if cluster_id:
        q = q.where(AuditEvent.target_id == cluster_id)
    if actor:
        q = q.where(AuditEvent.actor == actor)
    if action:
        q = q.where(AuditEvent.action.startswith(action))
    return list((await s.scalars(q)).all())


@router.get("/healthz", include_in_schema=False)
async def healthz() -> Response:
    return Response("ok")
