"""``cql-client`` command line.

Examples::

    cql-client --hosts 1.2.3.4,1.2.3.5 --username scylla --password '...' write 1000
    cql-client --from-api http://localhost:8000 --cluster c-123 --api-key dev-key verify
    cql-client --hosts ... demo        # write 10 rows, read them back, verify
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from cassandra.cluster import NoHostAvailable

from cql_client.api import ApiError, DbaasApi
from cql_client.connection import (
    DEFAULT_PORT,
    ConfigError,
    ConnectionSettings,
    build_cluster,
    cluster_datacenters,
    parse_hosts,
    pick_datacenter,
)
from cql_client.workload import Manifest, read_rows, verify_rows, write_rows

Connector = Callable[[ConnectionSettings], AbstractContextManager[tuple[Any, str]]]


@contextmanager
def connect(cfg: ConnectionSettings) -> Iterator[tuple[Any, str]]:
    """Yield ``(session, datacenter)``; always shuts the driver down."""
    cluster = build_cluster(cfg)
    try:
        session = cluster.connect()
        yield session, pick_datacenter(cluster_datacenters(cluster), cfg.dc)
    finally:
        cluster.shutdown()


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(
        prog="cql-client",
        description="Write, read and verify demo data in a Scylla DBaaS Cluster over CQL.",
    )
    g = p.add_argument_group("direct connection")
    g.add_argument("--hosts", default=env("CQL_HOSTS"), help="comma-separated contact points")
    g.add_argument("--port", type=int, default=int(env("CQL_PORT", str(DEFAULT_PORT))))
    g.add_argument("--username", default=env("CQL_USERNAME"))
    g.add_argument("--password", default=env("CQL_PASSWORD"))
    g.add_argument("--dc", default=env("CQL_DC"), help="datacenter name (default: auto-detect)")
    a = p.add_argument_group("connection info from the Control Plane API")
    a.add_argument("--from-api", metavar="URL", default=env("DBAAS_API_URL"))
    a.add_argument("--cluster", metavar="ID", default=env("DBAAS_CLUSTER_ID"))
    a.add_argument("--api-key", default=env("DBAAS_API_KEY"))
    p.add_argument("--manifest", type=Path, default=Path(env("CQL_MANIFEST", "cql-manifest.json")))
    p.add_argument("--concurrency", type=int, default=64)

    sub = p.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="write N rows to demo.kv and record them in the manifest")
    w.add_argument("count", type=int)
    r = sub.add_parser("read", help="print some rows of demo.kv")
    r.add_argument("--limit", type=int, default=10)
    sub.add_parser("verify", help="re-read the manifest rows; exit 1 on any mismatch")
    d = sub.add_parser("demo", help="write rows, read them back and verify")
    d.add_argument("--rows", type=int, default=10)
    return p


def resolve_settings(args: argparse.Namespace, api: DbaasApi | None = None) -> ConnectionSettings:
    cfg = ConnectionSettings(
        hosts=parse_hosts(args.hosts or ""),
        username=args.username,
        password=args.password,
        port=args.port,
        dc=args.dc,
    )
    if not args.from_api:
        return cfg
    if not args.cluster or not args.api_key:
        raise ConfigError("--from-api needs --cluster and --api-key")
    client = api or DbaasApi(args.from_api, args.api_key)
    try:
        info = client.connection(args.cluster)
    finally:
        if api is None:
            client.close()
    if info.password and not args.password:
        print(
            "note: the API revealed the CQL password on this first call and will not show "
            f"it again; keep it: {info.password}",
            file=sys.stderr,
        )
    return replace(
        cfg,
        hosts=cfg.hosts or info.contact_points,
        username=args.username or info.username,
        password=args.password or info.password,
        port=info.port if args.port == DEFAULT_PORT else args.port,
        address_map=info.address_map,
    )


def run(args: argparse.Namespace, cfg: ConnectionSettings, connector: Connector) -> int:
    with connector(cfg) as (session, dc):
        if args.command in ("write", "demo"):
            count = args.count if args.command == "write" else args.rows
            manifest = write_rows(session, count, dc, concurrency=args.concurrency)
            manifest.save(args.manifest)
            print(f"wrote {count} rows to demo.kv (dc={dc}, CL=QUORUM); manifest: {args.manifest}")
        if args.command in ("read", "demo"):
            limit = args.limit if args.command == "read" else args.rows
            for row_id, ts, payload in read_rows(session, limit):
                print(f"{row_id}  {ts.isoformat()}  {payload}")
        if args.command in ("verify", "demo"):
            manifest = Manifest.load(args.manifest)
            res = verify_rows(session, manifest, concurrency=args.concurrency)
            print(
                f"verified {res.checked} rows: {len(res.missing)} missing, "
                f"{len(res.mismatched)} mismatched"
            )
            for row_id in res.missing[:10]:
                print(f"  missing    {row_id}", file=sys.stderr)
            for row_id in res.mismatched[:10]:
                print(f"  mismatched {row_id}", file=sys.stderr)
            if not res.ok:
                return 1
    return 0


def main(argv: list[str] | None = None, connector: Connector = connect) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = resolve_settings(args)
        cfg.validate()
        return run(args, cfg, connector)
    except (ConfigError, ApiError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except NoHostAvailable as e:
        print(f"error: cannot reach the Cluster over CQL: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
