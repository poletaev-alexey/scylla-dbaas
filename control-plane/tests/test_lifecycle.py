from datetime import timedelta

from conftest import Env

from dbaas.models import utcnow


async def test_requires_api_key_and_audits_denial(env: Env) -> None:
    r = await env.client.get("/api/v1/clusters", headers={"X-API-Key": "nope"})
    assert r.status_code == 401
    events = (await env.client.get("/api/v1/audit-events?action=auth")).json()
    assert events[0]["result"] == "denied"


async def test_create_cluster_becomes_healthy(env: Env) -> None:
    cid = await env.create_cluster()
    c = (await env.client.get(f"/api/v1/clusters/{cid}")).json()
    assert c["status"] == "healthy"
    assert sorted(n["name"] for n in c["nodes"]) == ["data-1", "data-2", "data-3", "monitor"]
    assert all(n["status"] == "up" for n in c["nodes"])
    assert c["grafana_url"].endswith(":3000")
    assert c["created_at"].endswith(("Z", "+00:00"))

    ops = (await env.client.get(f"/api/v1/operations?cluster_id={cid}")).json()
    assert ops[0]["type"] == "create_cluster" and ops[0]["status"] == "succeeded"
    detail = (await env.client.get(f"/api/v1/operations/{ops[0]['id']}")).json()
    assert any("Data Nodes" in e["message"] for e in detail["log"])

    actions = [
        e["action"] for e in (await env.client.get(f"/api/v1/audit-events?cluster_id={cid}")).json()
    ]
    assert "create_cluster.requested" in actions and "create_cluster.completed" in actions


async def test_duplicate_name_rejected(env: Env) -> None:
    await env.create_cluster("demo")
    r = await env.client.post("/api/v1/clusters", json={"name": "demo"})
    assert r.status_code == 409


async def test_failed_provisioning_marks_cluster_failed(env: Env) -> None:
    env.world.fail_next.add("provision")
    cid = await env.create_cluster()
    c = (await env.client.get(f"/api/v1/clusters/{cid}")).json()
    assert c["status"] == "failed"
    ops = (await env.client.get(f"/api/v1/operations?cluster_id={cid}")).json()
    assert ops[0]["status"] == "failed" and "injected" in ops[0]["error"]


async def test_one_operation_per_cluster(env: Env) -> None:
    env.world.step_delay = 0.05
    cid = await env.create_cluster()
    r1 = await env.client.post(f"/api/v1/clusters/{cid}/rolling-restart")
    r2 = await env.client.post(f"/api/v1/clusters/{cid}/backups")
    assert r1.status_code == 202 and r2.status_code == 409
    assert r2.json()["active_operation_id"] == r1.json()["operation"]["id"]
    await env.wait()


async def test_connection_reveals_password_once(env: Env) -> None:
    cid = await env.create_cluster()
    first = (await env.client.get(f"/api/v1/clusters/{cid}/connection")).json()
    second = (await env.client.get(f"/api/v1/clusters/{cid}/connection")).json()
    assert first["password"] and second["password"] is None
    assert len(first["contact_points"]) == 3


async def test_delete_requires_confirmation_and_keeps_backups(env: Env) -> None:
    cid = await env.create_cluster()
    await env.client.post(f"/api/v1/clusters/{cid}/backups")
    await env.wait()
    r = await env.client.delete(f"/api/v1/clusters/{cid}", params={"confirm": "wrong"})
    assert r.status_code == 422
    r = await env.client.delete(f"/api/v1/clusters/{cid}", params={"confirm": "demo"})
    assert r.status_code == 202
    await env.wait()
    assert (await env.client.get(f"/api/v1/clusters/{cid}")).status_code == 404
    backups = (await env.client.get("/api/v1/backups")).json()
    assert len(backups) == 1 and backups[0]["status"] == "done"


async def test_backup_and_restore_into_new_cluster(env: Env) -> None:
    cid = await env.create_cluster("source")
    env.world.clusters[cid].tables["demo.kv"] = ["k1=v1", "k2=v2"]
    await env.client.post(f"/api/v1/clusters/{cid}/backups")
    await env.wait()
    backup = (await env.client.get(f"/api/v1/clusters/{cid}/backups")).json()[0]
    assert backup["status"] == "done" and backup["snapshot_tag"]

    await env.client.delete(f"/api/v1/clusters/{cid}", params={"confirm": "source"})
    await env.wait()

    new_id = await env.create_cluster("restored", from_backup_id=backup["id"])
    c = (await env.client.get(f"/api/v1/clusters/{new_id}")).json()
    assert c["status"] == "healthy" and c["source_backup_id"] == backup["id"]
    assert env.world.clusters[new_id].tables["demo.kv"] == ["k1=v1", "k2=v2"]


async def test_auto_recovery_replaces_dead_data_node(env: Env) -> None:
    cid = await env.create_cluster()
    old = env.world.clusters[cid].nodes["data-2"].instance_id
    env.world.kill_node(cid, "data-2")

    await env.health.check_once()
    c = (await env.client.get(f"/api/v1/clusters/{cid}")).json()
    assert c["status"] == "degraded"
    assert env.runner._tasks == {}  # still inside the grace period

    await env.health.check_once(now=utcnow() + timedelta(seconds=120))
    await env.wait()
    c = (await env.client.get(f"/api/v1/clusters/{cid}")).json()
    assert c["status"] == "healthy"
    assert env.world.clusters[cid].nodes["data-2"].instance_id != old

    ops = (await env.client.get(f"/api/v1/operations?cluster_id={cid}")).json()
    assert ops[0]["type"] == "replace_node" and ops[0]["actor"] == "system"


async def test_auto_recovery_preempts_backup(env: Env) -> None:
    cid = await env.create_cluster()
    env.world.step_delay = 0.2
    await env.client.post(f"/api/v1/clusters/{cid}/backups")
    env.world.kill_node(cid, "data-1")
    await env.health.check_once()
    await env.health.check_once(now=utcnow() + timedelta(seconds=120))
    await env.wait()
    ops = {
        o["type"]: o for o in (await env.client.get(f"/api/v1/operations?cluster_id={cid}")).json()
    }
    assert ops["backup"]["status"] == "cancelled"
    assert ops["replace_node"]["status"] == "succeeded"
    backups = (await env.client.get(f"/api/v1/clusters/{cid}/backups")).json()
    # Pre-emption may land before the Backup row is written; either way none may be left
    # dangling as "running" or claim to be "done".
    assert all(b["status"] == "failed" for b in backups)


async def test_auto_recovery_can_be_disabled(env: Env) -> None:
    cid = await env.create_cluster()
    await env.client.patch(f"/api/v1/clusters/{cid}", json={"auto_recovery": False})
    env.world.kill_node(cid, "data-3")
    await env.health.check_once()
    await env.health.check_once(now=utcnow() + timedelta(seconds=600))
    await env.wait()
    c = (await env.client.get(f"/api/v1/clusters/{cid}")).json()
    assert c["status"] == "degraded"


async def test_lost_monitor_node_degrades_and_is_recreated(env: Env) -> None:
    cid = await env.create_cluster()
    env.world.kill_monitor(cid)
    await env.health.check_once()
    assert (await env.client.get(f"/api/v1/clusters/{cid}")).json()["status"] == "degraded"
    await env.health.check_once(now=utcnow() + timedelta(seconds=120))
    await env.wait()
    assert (await env.client.get(f"/api/v1/clusters/{cid}")).json()["status"] == "healthy"


async def test_openapi_exposes_all_resources(env: Env) -> None:
    paths = (await env.client.get("/openapi.json")).json()["paths"]
    for p in [
        "/api/v1/clusters",
        "/api/v1/clusters/{cluster_id}/backups",
        "/api/v1/operations/{operation_id}",
        "/api/v1/audit-events",
    ]:
        assert p in paths


async def test_scheduled_backups_are_discovered_and_purges_mirrored(env: Env) -> None:
    from dbaas.providers.fake import FakeClusterOps

    cid = await env.create_cluster()
    ops = env.runner.provider.ops
    assert isinstance(ops, FakeClusterOps)
    tag = ops.scheduled_backup(cid)
    await env.health.sync_backups_once()
    backups = (await env.client.get(f"/api/v1/clusters/{cid}/backups")).json()
    assert [(b["snapshot_tag"], b["scheduled"], b["status"]) for b in backups] == [
        (tag, True, "done")
    ]

    await env.health.sync_backups_once()  # idempotent
    assert len((await env.client.get(f"/api/v1/clusters/{cid}/backups")).json()) == 1

    env.world.snapshots.clear()  # Manager retention purged it
    await env.health.sync_backups_once()
    backups = (await env.client.get(f"/api/v1/clusters/{cid}/backups")).json()
    assert backups[0]["status"] == "deleted"


async def test_restart_does_not_leave_cluster_in_transitional_state(env: Env) -> None:
    """Found on the first AWS run: after a restart the Operation was failed but the
    Cluster stayed `provisioning` forever."""
    from dbaas.domain import ClusterStatus, OperationStatus, OperationType
    from dbaas.models import Cluster, Operation

    cid = await env.create_cluster("half-built")
    other = await env.create_cluster("mid-recovery")
    async with env.runner.sessions() as s:
        for c_id, c_status, op_type in (
            (cid, ClusterStatus.PROVISIONING, OperationType.CREATE_CLUSTER),
            (other, ClusterStatus.RECOVERING, OperationType.REPLACE_NODE),
        ):
            c = await s.get(Cluster, c_id)
            assert c is not None
            c.status = c_status
            s.add(
                Operation(
                    id=f"op-stale-{c_id}",
                    cluster_id=c_id,
                    type=op_type,
                    status=OperationStatus.RUNNING,
                    actor="alice",
                    params={},
                )
            )
        await s.commit()

    await env.runner.recover_after_restart()

    assert (await env.client.get(f"/api/v1/clusters/{cid}")).json()["status"] == "failed"
    assert (await env.client.get(f"/api/v1/clusters/{other}")).json()["status"] == "degraded"
    op = (await env.client.get(f"/api/v1/operations/op-stale-{cid}")).json()
    assert op["status"] == "failed"
