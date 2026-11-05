"""``dbaas-e2e``: end-to-end scenario against a running Control Plane.

create Cluster -> write rows over CQL -> verify -> [Backup -> Restore into a new Cluster
-> verify the restored data] -> delete every Cluster it created (always, even on failure).

Used by ``make demo-local`` (local driver) and the manual ``aws-e2e`` workflow (aws driver).
Cluster ids are appended to ``--state-file`` as soon as they are known, so CI can run a
safety-net cleanup if this process dies.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

from cql_client.api import ApiError, DbaasApi
from cql_client.cli import Connector, connect
from cql_client.connection import ConnectionSettings
from cql_client.workload import Manifest, verify_rows, write_rows


def _log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


class Scenario:
    def __init__(
        self,
        api: DbaasApi,
        *,
        connector: Connector = connect,
        timeout: float = 3600.0,
        poll: float = 10.0,
        state_file: Path | None = None,
        log: Callable[[str], None] = _log,
    ) -> None:
        self.api, self.connector = api, connector
        self.timeout, self.poll = timeout, poll
        self.state_file, self.log = state_file, log
        self.created: list[tuple[str, str]] = []  # (cluster_id, name)
        self.backed_up: list[str] = []  # cluster ids whose Backups we must delete

    def _wait(self, operation_id: str, what: str) -> dict[str, object]:
        self.log(f"waiting for {what} (Operation {operation_id})")
        return self.api.wait_operation(
            operation_id,
            timeout=self.timeout,
            poll=self.poll,
            on_log=lambda m: self.log(f"  {m}"),
        )

    def create(self, name: str, from_backup_id: str | None = None) -> str:
        cluster_id, op = self.api.create_cluster(name, from_backup_id)
        self.created.append((cluster_id, name))
        if self.state_file is not None:
            with self.state_file.open("a") as f:
                f.write(f"{cluster_id} {name}\n")
        self._wait(op, f"create Cluster {name} ({cluster_id})")
        return cluster_id

    def settings(self, cluster_id: str) -> ConnectionSettings:
        info = self.api.connection(cluster_id)
        return ConnectionSettings(
            hosts=info.contact_points,
            username=info.username,
            password=info.password,
            port=info.port,
            address_map=info.address_map,
        )

    def write_and_verify(self, cluster_id: str, rows: int) -> Manifest:
        with self.connector(self.settings(cluster_id)) as (session, dc):
            manifest = write_rows(session, rows, dc)
            self.log(f"wrote {rows} rows (dc={dc})")
            self._verify(session, manifest, "source Cluster")
        return manifest

    def verify(self, cluster_id: str, manifest: Manifest) -> None:
        with self.connector(self.settings(cluster_id)) as (session, _dc):
            self._verify(session, manifest, "restored Cluster")

    def _verify(self, session: object, manifest: Manifest, where: str) -> None:
        res = verify_rows(session, manifest)
        self.log(
            f"verify on {where}: {res.checked} checked, {len(res.missing)} missing, "
            f"{len(res.mismatched)} mismatched"
        )
        if not res.ok:
            raise AssertionError(f"data verification failed on {where}")

    def backup(self, cluster_id: str) -> str:
        self.backed_up.append(cluster_id)
        op = self._wait(self.api.backup_now(cluster_id), "Backup")
        params = op.get("params")
        backup_id = params.get("backup_id") if isinstance(params, dict) else None
        if not backup_id:
            raise ApiError("Backup Operation succeeded but reported no backup_id")
        self.log(f"Backup {backup_id} done")
        return str(backup_id)

    def cleanup(self) -> bool:
        ok = True
        for cluster_id, name in reversed(self.created):
            try:
                self._wait(self.api.delete_cluster(cluster_id, name), f"delete {name}")
            except Exception as e:
                ok = False
                self.log(f"CLEANUP FAILED for {cluster_id}: {e}")
        # Backups outlive their Cluster (ADR-0005); the scenario removes its own.
        for cluster_id in self.backed_up:
            try:
                self._wait(self.api.delete_backups(cluster_id), f"delete Backups of {cluster_id}")
            except Exception as e:
                ok = False
                self.log(f"CLEANUP FAILED for Backups of {cluster_id}: {e}")
        return ok

    def run(self, name: str, rows: int, with_restore: bool) -> None:
        source = self.create(name)
        manifest = self.write_and_verify(source, rows)
        if with_restore:
            backup_id = self.backup(source)
            restored = self.create(f"{name}-r", from_backup_id=backup_id)
            self.verify(restored, manifest)


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    p = argparse.ArgumentParser(
        prog="dbaas-e2e", description="End-to-end scenario against a running Control Plane."
    )
    p.add_argument("--api", default=env("DBAAS_API_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=env("DBAAS_API_KEY", "dev-key"))
    p.add_argument("--name", default=f"e2e-{secrets.token_hex(3)}")
    p.add_argument("--rows", type=int, default=1000)
    p.add_argument("--with-restore", action="store_true")
    p.add_argument("--timeout", type=float, default=3600.0, help="per Operation, seconds")
    p.add_argument("--poll", type=float, default=10.0)
    p.add_argument("--state-file", type=Path)
    p.add_argument("--keep", action="store_true", help="do not delete the Clusters at the end")
    args = p.parse_args(argv)

    with DbaasApi(args.api, args.api_key) as api:
        sc = Scenario(api, timeout=args.timeout, poll=args.poll, state_file=args.state_file)
        failed = False
        try:
            sc.run(args.name, args.rows, args.with_restore)
            _log("scenario PASSED")
        except Exception as e:
            failed = True
            _log(f"scenario FAILED: {e}")
        finally:
            cleaned = True if args.keep else sc.cleanup()
    return 1 if failed or not cleaned else 0


if __name__ == "__main__":
    sys.exit(main())
