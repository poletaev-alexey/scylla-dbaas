# Agent: scylla-ops-local

Goal: the real `ClusterOps` (shared by aws and local drivers) and the `local` Provider driver.

Own: `control-plane/src/dbaas/scylla/`, `control-plane/src/dbaas/providers/local.py`,
`deploy/local/` (docker-compose for local Clusters + MinIO), `docker-compose.yml` at the repo
root (Control Plane + UI dev stack), `control-plane/Dockerfile`, `control-plane/tests/test_scylla_ops.py`,
`control-plane/tests/integration/`.

1. `dbaas/scylla/manager.py`: async httpx client for the Scylla Manager REST API (verify
   endpoints in the official docs / source: cluster add, task create for backup/repair/restore,
   task progress polling, backup listing). `dbaas/scylla/rest.py`: Scylla REST API client
   (port 10000) for node liveness (`/gossiper/endpoint/live`, `/storage_service/...`) and
   drain/restart helpers if needed.
2. `dbaas/scylla/ops.py`: `ManagerClusterOps` implementing `ClusterOps`:
   health (Scylla REST from any reachable node + Manager status), register_in_manager,
   set_backup_schedule (Manager backup task with interval + retention), backup (ad-hoc backup
   task, wait, return snapshot tag), restore (restore schema task, then rolling restart as
   Manager docs require, then data restore task), repair, rolling_restart (how to restart
   Scylla without SSH: via SSM `AWS-RunShellScript` on aws, `docker restart` on local — put
   the restart mechanism behind a small injected callable), delete_backups.
3. `providers/local.py`: `LocalInfrastructure` using the Docker Engine via the `docker` CLI
   (async subprocess) — 3 `scylladb/scylla` containers + 1 Monitor container set
   (scylla-manager, its Scylla backend, Prometheus, Grafana) on a per-Cluster network, MinIO
   as S3. Keep resource flags small (`--smp 1 --memory 750M --overprovisioned 1`).
   Node Replacement = remove the container and start a new one with
   `--replace-node-first-boot <host_id>` (verify the flag). Expose `build(settings)`.
4. Tests: unit tests of the clients with `httpx.MockTransport`. An integration test
   (`tests/integration/`, marked `@pytest.mark.integration`, skipped unless
   `DBAAS_INTEGRATION=1`) that drives the API through create → backup → delete → restore on the
   local driver. Docker may not be running on this machine: if not, write the test, verify
   what you can, and say so clearly.
