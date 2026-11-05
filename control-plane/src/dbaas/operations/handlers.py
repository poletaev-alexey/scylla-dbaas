"""What each Operation type actually does. One function per OperationType."""

from sqlalchemy import update

from dbaas.domain import BackupStatus, ClusterStatus, NodeRole, NodeStatus, OperationType
from dbaas.models import Backup, Cluster, Node, new_id, utcnow
from dbaas.operations.runner import Handler, OperationContext
from dbaas.providers.base import ClusterInfra, InstanceState, NodeInfo, SnapshotRef


def infra_of(cluster: Cluster) -> ClusterInfra:
    """Rebuild the provider view of a Cluster from what the Control Plane persisted."""
    return ClusterInfra(
        nodes=[
            NodeInfo(
                name=n.name,
                role=n.role,
                instance_id=n.instance_id or "",
                state=InstanceState.RUNNING,
                public_ip=n.public_ip,
                private_ip=n.private_ip,
            )
            for n in cluster.nodes
        ],
        grafana_url=cluster.grafana_url or "",
        manager_url=cluster.manager_url or "",
        credentials_ref=cluster.credentials_ref or "",
        backup_location=cluster.backup_location or "",
    )


async def _cluster(ctx: OperationContext) -> Cluster:
    assert ctx.cluster_id is not None
    c = await ctx.session.get(Cluster, ctx.cluster_id, populate_existing=True)
    if c is None:
        raise LookupError(f"Cluster {ctx.cluster_id} not found")
    return c


def _apply_node(cluster: Cluster, info: NodeInfo) -> None:
    node = next((n for n in cluster.nodes if n.name == info.name), None)
    if node is None:
        node = Node(name=info.name, role=info.role)
        cluster.nodes.append(node)
    node.instance_id = info.instance_id
    node.public_ip = info.public_ip
    node.private_ip = info.private_ip
    node.status = NodeStatus.UP if info.state is InstanceState.RUNNING else NodeStatus.PENDING
    node.down_since = None


async def create_cluster(ctx: OperationContext) -> None:
    c = await _cluster(ctx)
    try:
        infra = await ctx.provider.infra.provision(c.id, c.name, ctx.step_log)
        for n in infra.nodes:
            _apply_node(c, n)
        c.grafana_url, c.manager_url = infra.grafana_url, infra.manager_url
        c.credentials_ref, c.backup_location = infra.credentials_ref, infra.backup_location
        await ctx.session.commit()

        c.manager_cluster_id = await ctx.provider.ops.register_in_manager(
            infra, c.name, ctx.step_log
        )
        await ctx.provider.ops.set_backup_schedule(
            infra, c.manager_cluster_id, c.backup_interval_hours, c.backup_retention
        )
        await ctx.log(
            f"Backup Schedule: every {c.backup_interval_hours}h, keep {c.backup_retention}"
        )

        if c.source_backup_id:
            backup = await ctx.session.get(Backup, c.source_backup_id)
            if backup is None or backup.snapshot_tag is None:
                raise LookupError(f"Backup {c.source_backup_id} is not restorable")
            await ctx.log(f"Restore from Backup {backup.id} ({backup.cluster_name})")
            await ctx.provider.ops.restore(
                infra,
                c.manager_cluster_id,
                SnapshotRef(snapshot_tag=backup.snapshot_tag, location=backup.location),
                ctx.step_log,
            )

        c.status = ClusterStatus.HEALTHY
        await ctx.session.commit()
    except Exception:
        await ctx.session.rollback()
        c = await _cluster(ctx)
        c.status = ClusterStatus.FAILED
        await ctx.session.commit()
        raise


async def delete_cluster(ctx: OperationContext) -> None:
    c = await _cluster(ctx)
    c.status = ClusterStatus.DELETING
    await ctx.session.commit()
    await ctx.provider.infra.destroy(c.id, ctx.step_log)
    for n in c.nodes:
        n.status = NodeStatus.TERMINATED
    c.status = ClusterStatus.DELETED
    await ctx.session.commit()
    await ctx.log("Backups are kept; delete them explicitly via DELETE /clusters/{id}/backups")


async def replace_node(ctx: OperationContext) -> None:
    c = await _cluster(ctx)
    node_name: str = ctx.params["node"]
    node = next(n for n in c.nodes if n.name == node_name)
    if node.role is not NodeRole.DATA:
        raise ValueError("only Data Nodes can be replaced; use recreate_monitor")
    c.status = ClusterStatus.RECOVERING
    node.status = NodeStatus.REPLACING
    await ctx.session.commit()

    info = await ctx.provider.infra.replace_node(c.id, node_name, ctx.step_log)
    _apply_node(c, info)
    await ctx.session.commit()
    if c.manager_cluster_id:
        await ctx.provider.ops.repair(infra_of(c), c.manager_cluster_id, ctx.step_log)
    c.status = ClusterStatus.HEALTHY
    await ctx.session.commit()


async def recreate_monitor(ctx: OperationContext) -> None:
    c = await _cluster(ctx)
    c.status = ClusterStatus.RECOVERING
    await ctx.session.commit()
    infra = await ctx.provider.infra.recreate_monitor(c.id, ctx.step_log)
    _apply_node(c, infra.monitor)
    c.grafana_url, c.manager_url = infra.grafana_url, infra.manager_url
    c.manager_cluster_id = await ctx.provider.ops.register_in_manager(infra, c.name, ctx.step_log)
    await ctx.provider.ops.set_backup_schedule(
        infra, c.manager_cluster_id, c.backup_interval_hours, c.backup_retention
    )
    c.status = ClusterStatus.HEALTHY
    await ctx.session.commit()


async def rolling_restart(ctx: OperationContext) -> None:
    c = await _cluster(ctx)
    await ctx.provider.ops.rolling_restart(infra_of(c), ctx.step_log)


async def backup(ctx: OperationContext) -> None:
    c = await _cluster(ctx)
    if c.manager_cluster_id is None:
        raise RuntimeError("Cluster is not registered in Scylla Manager")
    manager_cluster_id = c.manager_cluster_id
    infra = infra_of(c)
    backup_id = new_id("b")  # known up front so failure handling never depends on a flush
    try:
        b = Backup(
            id=backup_id,
            cluster_id=c.id,
            cluster_name=c.name,
            location=c.backup_location or "",
            scheduled=bool(ctx.params.get("scheduled", False)),
        )
        ctx.session.add(b)
        ctx.op.params = {**ctx.params, "backup_id": backup_id}
        await ctx.session.commit()
        snap = await ctx.provider.ops.backup(infra, manager_cluster_id, ctx.step_log)
        b.snapshot_tag = snap.snapshot_tag
        b.location = snap.location
        b.status = BackupStatus.DONE
        b.finished_at = utcnow()
        await ctx.session.commit()
    except BaseException:
        # Also reached on cancellation (Auto-recovery pre-empting us), possibly mid-commit,
        # so use a fresh session: the operation's own one may be unusable.
        async with ctx.runner.sessions() as s:
            await s.execute(
                update(Backup)
                .where(Backup.id == backup_id, Backup.status == BackupStatus.RUNNING)
                .values(status=BackupStatus.FAILED, finished_at=utcnow())
            )
            await s.commit()
        raise


async def delete_backups(ctx: OperationContext) -> None:
    location: str = ctx.params["location"]
    await ctx.provider.ops.delete_backups(location, ctx.step_log)
    await ctx.session.execute(
        update(Backup)
        .where(Backup.cluster_id == ctx.params["cluster_id"])
        .values(status=BackupStatus.DELETED)
    )
    await ctx.session.commit()


HANDLERS: dict[OperationType, Handler] = {
    OperationType.CREATE_CLUSTER: create_cluster,
    OperationType.DELETE_CLUSTER: delete_cluster,
    OperationType.REPLACE_NODE: replace_node,
    OperationType.RECREATE_MONITOR: recreate_monitor,
    OperationType.ROLLING_RESTART: rolling_restart,
    OperationType.BACKUP: backup,
    OperationType.DELETE_BACKUPS: delete_backups,
}
