"""``aws`` Provider driver (ADR-0002, ADR-0006).

``AwsInfrastructure`` drives the per-Cluster Terraform stack in ``infra/cluster`` as an async
subprocess: one Terraform state per Cluster (S3 backend key ``clusters/<id>.tfstate``) and one
working directory per Cluster (``TF_DATA_DIR`` + the tfvars file) under a configurable root.
Node Replacement is ``apply -replace='aws_instance.data["data-N"]'`` with the dead node's Scylla
Host ID passed in so cloud-init sets ``replace_node_first_boot``.

Cheap reads do not touch Terraform: ``describe()`` uses EC2 ``DescribeInstances`` filtered by the
``dbaas:cluster_id`` tag and ``get_credentials()`` reads the Secrets Manager secret.

boto3 is an optional dependency (``pip install dbaas[aws]``); it is imported lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from dbaas.domain import NodeRole
from dbaas.providers.base import (
    ClusterInfra,
    ClusterOps,
    Credentials,
    InstanceState,
    NodeInfo,
    Provider,
    StepLog,
)

if TYPE_CHECKING:
    from dbaas.config import Settings

DATA_NODE_NAMES = ("data-1", "data-2", "data-3")
MONITOR_NAME = "monitor"

_EC2_STATES: dict[str, InstanceState] = {
    "pending": InstanceState.PENDING,
    "running": InstanceState.RUNNING,
    "stopping": InstanceState.STOPPED,
    "stopped": InstanceState.STOPPED,
    "shutting-down": InstanceState.TERMINATED,
    "terminated": InstanceState.TERMINATED,
}

# Terraform lines worth showing even when the log is being throttled.
_IMPORTANT = (
    "Error",
    "error",
    "Warning",
    "complete",
    "Apply complete",
    "Destroy complete",
    "Plan:",
)


class TerraformError(RuntimeError):
    """Terraform exited non-zero. ``tail`` holds the last output lines for the Operation log."""

    def __init__(self, args: Sequence[str], code: int, tail: list[str]) -> None:
        self.code = code
        self.tail = tail
        joined = "\n".join(tail)
        super().__init__(f"terraform {' '.join(args)} exited {code}:\n{joined}")


class AwsDriverSettings(BaseSettings):
    """Driver-only knobs not present in the shared ``Settings`` (env prefix ``DBAAS_AWS_``)."""

    model_config = SettingsConfigDict(env_prefix="DBAAS_AWS_", env_file=".env", extra="ignore")

    terraform_bin: str = "terraform"
    work_root: str = "./.terraform-work"
    name_prefix: str = "dbaas"
    ready_timeout_seconds: float = 2700.0
    ready_poll_seconds: float = 15.0
    log_interval_seconds: float = 5.0


@dataclass(frozen=True)
class AwsConfig:
    region: str
    operator_cidr: str
    module_dir: Path
    state_bucket: str
    backup_bucket: str
    work_root: Path
    terraform_bin: str = "terraform"
    name_prefix: str = "dbaas"
    ready_timeout_seconds: float = 2700.0
    ready_poll_seconds: float = 15.0
    log_interval_seconds: float = 5.0
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(
        cls, settings: Settings, driver: AwsDriverSettings | None = None
    ) -> AwsConfig:
        d = driver or AwsDriverSettings()
        return cls(
            region=settings.aws_region,
            operator_cidr=settings.operator_cidr,
            module_dir=Path(settings.terraform_dir).resolve(),
            state_bucket=settings.terraform_state_bucket,
            backup_bucket=settings.backup_bucket,
            work_root=Path(d.work_root).resolve(),
            terraform_bin=d.terraform_bin,
            name_prefix=d.name_prefix,
            ready_timeout_seconds=d.ready_timeout_seconds,
            ready_poll_seconds=d.ready_poll_seconds,
            log_interval_seconds=d.log_interval_seconds,
        )


class ThrottledLog:
    """Forwards Terraform output to a StepLog without flooding the Operation log.

    Important lines (errors, "... complete") always pass; others pass at most once per
    ``interval`` seconds, with a count of the lines that were skipped in between.
    """

    def __init__(self, log: StepLog, interval: float, prefix: str = "terraform: ") -> None:
        self._log = log
        self._interval = interval
        self._prefix = prefix
        self._last = float("-inf")
        self._skipped = 0

    async def line(self, text: str) -> None:
        text = text.rstrip()
        if not text:
            return
        now = time.monotonic()
        if any(k in text for k in _IMPORTANT) or now - self._last >= self._interval:
            suffix = f" (+{self._skipped} lines)" if self._skipped else ""
            await self._log(f"{self._prefix}{text}{suffix}")
            self._last, self._skipped = now, 0
        else:
            self._skipped += 1


HostIdLookup = Callable[[Sequence[str], str], Awaitable[str]]
PortProbe = Callable[[str, int], Awaitable[bool]]


async def scylla_host_id_lookup(peer_hosts: Sequence[str], dead_ip: str) -> str:
    """Host ID of the node whose broadcast address is ``dead_ip``, asked from any live peer via
    the Scylla REST API (``GET /storage_service/host_id`` -> ``[{"key": ip, "value": id}]``)."""
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for host in peer_hosts:
            try:
                r = await client.get(f"http://{host}:10000/storage_service/host_id")
                r.raise_for_status()
                for entry in r.json():
                    if entry.get("key") == dead_ip:
                        return str(entry["value"])
                errors.append(f"{host}: {dead_ip} not in host_id map")
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{host}: {e}")
    raise LookupError(f"could not find Scylla Host ID of {dead_ip}: {'; '.join(errors)}")


async def tcp_port_open(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def _boto3_client(service: str, region: str) -> Any:
    boto3: Any = importlib.import_module("boto3")  # optional dependency, imported lazily
    return boto3.client(service, region_name=region)


class AwsInfrastructure:
    """``Infrastructure`` implementation on EC2 via Terraform (see module docstring)."""

    def __init__(
        self,
        config: AwsConfig,
        *,
        client_factory: Callable[[str, str], Any] = _boto3_client,
        host_id_lookup: HostIdLookup = scylla_host_id_lookup,
        port_probe: PortProbe = tcp_port_open,
    ) -> None:
        self.cfg = config
        self._client = client_factory
        self._host_id_lookup = host_id_lookup
        self._port_probe = port_probe
        self._locks: dict[str, asyncio.Lock] = {}

    # --- working directory / terraform ---------------------------------------------------

    def work_dir(self, cluster_id: str) -> Path:
        if not cluster_id or "/" in cluster_id or cluster_id.startswith("."):
            raise ValueError(f"invalid cluster id {cluster_id!r}")
        return self.cfg.work_root / cluster_id

    def _tfvars_path(self, cluster_id: str) -> Path:
        return self.work_dir(cluster_id) / "cluster.tfvars.json"

    def _write_tfvars(
        self, cluster_id: str, name: str | None, replace: dict[str, str] | None = None
    ) -> Path:
        path = self._tfvars_path(cluster_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if name is None:  # keep the name from provision(); fall back to the id
            try:
                name = str(json.loads(path.read_text())["cluster_name"])
            except (OSError, ValueError, KeyError):
                name = cluster_id
        tfvars: dict[str, Any] = {
            "region": self.cfg.region,
            "name_prefix": self.cfg.name_prefix,
            "cluster_id": cluster_id,
            "cluster_name": name,
            "operator_cidr": self.cfg.operator_cidr,
            "backup_bucket": self.cfg.backup_bucket,
            "replace_node_host_ids": replace or {},
        }
        path.write_text(json.dumps(tfvars, indent=2))
        return path

    def _lock(self, cluster_id: str) -> asyncio.Lock:
        return self._locks.setdefault(cluster_id, asyncio.Lock())

    async def _terraform(self, cluster_id: str, args: Sequence[str], log: StepLog | None) -> str:
        """Run terraform in the module dir with a per-Cluster TF_DATA_DIR.

        Output is streamed line by line (throttled) to ``log``; when ``log`` is None the stdout
        is captured and returned (used for ``output -json``).
        """
        work = self.work_dir(cluster_id)
        work.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            **self.cfg.extra_env,
            "TF_DATA_DIR": str(work / ".terraform"),
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
        }
        cmd = [self.cfg.terraform_bin, f"-chdir={self.cfg.module_dir}", *args]
        if log is None:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                err_tail = err.decode(errors="replace").splitlines()[-30:]
                raise TerraformError(args, proc.returncode or 1, err_tail)
            return out.decode(errors="replace")

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
        )
        assert proc.stdout is not None
        throttled = ThrottledLog(log, self.cfg.log_interval_seconds)
        tail: list[str] = []
        async for raw in proc.stdout:
            text = raw.decode(errors="replace")
            await throttled.line(text)
            tail = [*tail[-29:], text.rstrip()]
        code = await proc.wait()
        if code != 0:
            raise TerraformError(args, code, tail)
        return ""

    async def _init(self, cluster_id: str, log: StepLog) -> None:
        await self._terraform(
            cluster_id,
            [
                "init",
                "-input=false",
                "-no-color",
                "-reconfigure",
                f"-backend-config=bucket={self.cfg.state_bucket}",
                f"-backend-config=key=clusters/{cluster_id}.tfstate",
                f"-backend-config=region={self.cfg.region}",
            ],
            log,
        )

    async def _apply(
        self, cluster_id: str, tfvars: Path, log: StepLog, extra: Sequence[str] = ()
    ) -> None:
        await self._terraform(
            cluster_id,
            [
                "apply",
                "-input=false",
                "-no-color",
                "-auto-approve",
                f"-var-file={tfvars}",
                *extra,
            ],
            log,
        )

    async def _outputs(self, cluster_id: str) -> ClusterInfra:
        raw = await self._terraform(cluster_id, ["output", "-json", "-no-color"], None)
        out = {k: v["value"] for k, v in json.loads(raw).items()}
        nodes = [
            NodeInfo(
                name=n["name"],
                role=NodeRole(n["role"]),
                instance_id=n["instance_id"],
                state=InstanceState.RUNNING,
                public_ip=n.get("public_ip"),
                private_ip=n.get("private_ip"),
            )
            for n in sorted(out["nodes"].values(), key=lambda n: str(n["name"]))
        ]
        return ClusterInfra(
            nodes=nodes,
            grafana_url=out["grafana_url"],
            manager_url=out["manager_url"],
            credentials_ref=out["secret_arn"],
            backup_location=out["backup_location"],
        )

    async def _wait_ready(self, nodes: Sequence[NodeInfo], log: StepLog) -> None:
        """Wait until every Data Node serves CQL and the Monitor Node serves the Manager API.

        cloud-init on a Data Node only opens 9042 after ``scylla-server`` has joined, so an
        open CQL port is a cheap, credential-free "Scylla is up" signal."""
        targets = [(n, 9042 if n.role is NodeRole.DATA else 5080) for n in nodes if n.public_ip]
        deadline = time.monotonic() + self.cfg.ready_timeout_seconds
        pending = list(targets)
        while pending:
            still = []
            for node, port in pending:
                assert node.public_ip is not None
                if await self._port_probe(node.public_ip, port):
                    await log(f"{node.name} is serving on :{port}")
                else:
                    still.append((node, port))
            pending = still
            if not pending:
                break
            if time.monotonic() >= deadline:
                names = ", ".join(f"{n.name}:{p}" for n, p in pending)
                raise TimeoutError(f"nodes not ready after bootstrap timeout: {names}")
            await log("waiting for cloud-init on " + ", ".join(n.name for n, _ in pending))
            await asyncio.sleep(self.cfg.ready_poll_seconds)

    # --- Infrastructure protocol ----------------------------------------------------------

    async def provision(self, cluster_id: str, name: str, log: StepLog) -> ClusterInfra:
        async with self._lock(cluster_id):
            tfvars = self._write_tfvars(cluster_id, name)
            await log(f"terraform init (state clusters/{cluster_id}.tfstate)")
            await self._init(cluster_id, log)
            await log("terraform apply: 3 Data Nodes + Monitor Node")
            await self._apply(cluster_id, tfvars, log)
            infra = await self._outputs(cluster_id)
            await log("waiting for ScyllaDB and Scylla Manager to come up (cloud-init)")
            await self._wait_ready(infra.nodes, log)
            return infra

    async def destroy(self, cluster_id: str, log: StepLog) -> None:
        async with self._lock(cluster_id):
            tfvars = self._write_tfvars(cluster_id, None)
            await self._init(cluster_id, log)
            await log("terraform destroy (Backups in S3 are kept)")
            await self._terraform(
                cluster_id,
                [
                    "destroy",
                    "-input=false",
                    "-no-color",
                    "-auto-approve",
                    f"-var-file={tfvars}",
                ],
                log,
            )
            shutil.rmtree(self.work_dir(cluster_id), ignore_errors=True)

    async def replace_node(self, cluster_id: str, node_name: str, log: StepLog) -> NodeInfo:
        if node_name not in DATA_NODE_NAMES:
            raise ValueError(f"{node_name!r} is not a Data Node; use recreate_monitor")
        async with self._lock(cluster_id):
            await self._init(cluster_id, log)
            before = await self._outputs(cluster_id)
            dead = next(n for n in before.nodes if n.name == node_name)
            assert dead.private_ip is not None
            peers = [
                n.public_ip
                for n in before.data_nodes
                if n.name != node_name and n.public_ip is not None
            ]
            host_id = await self._host_id_lookup(peers, dead.private_ip)
            await log(f"{node_name} ({dead.private_ip}) has Scylla Host ID {host_id}")
            tfvars = self._write_tfvars(cluster_id, None, replace={node_name: host_id})
            try:
                await log(f"terraform apply -replace for {node_name} (replace_node_first_boot)")
                await self._apply(
                    cluster_id, tfvars, log, [f'-replace=aws_instance.data["{node_name}"]']
                )
            finally:
                # ignore_changes on user_data keeps later applies from touching the node.
                self._write_tfvars(cluster_id, None)
            after = await self._outputs(cluster_id)
            new = next(n for n in after.nodes if n.name == node_name)
            await log(f"waiting for {node_name} to finish streaming and serve CQL")
            await self._wait_ready([new], log)
            return new

    async def recreate_monitor(self, cluster_id: str, log: StepLog) -> NodeInfo:
        async with self._lock(cluster_id):
            tfvars = self._write_tfvars(cluster_id, None)
            await self._init(cluster_id, log)
            await log("terraform apply -replace for the Monitor Node")
            await self._apply(cluster_id, tfvars, log, ["-replace=aws_instance.monitor"])
            after = await self._outputs(cluster_id)
            mon = after.monitor
            await self._wait_ready([mon], log)
            return mon

    async def describe(self, cluster_id: str) -> list[NodeInfo]:
        return await asyncio.to_thread(self._describe_sync, cluster_id)

    def _describe_sync(self, cluster_id: str) -> list[NodeInfo]:
        ec2 = self._client("ec2", self.cfg.region)
        paginator = ec2.get_paginator("describe_instances")
        best: dict[str, tuple[tuple[int, float], NodeInfo]] = {}
        for page in paginator.paginate(
            Filters=[{"Name": "tag:dbaas:cluster_id", "Values": [cluster_id]}]
        ):
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    name = tags.get("dbaas:node")
                    if not name:
                        continue
                    state = _EC2_STATES.get(inst["State"]["Name"], InstanceState.MISSING)
                    role = NodeRole.MONITOR if name == MONITOR_NAME else NodeRole.DATA
                    info = NodeInfo(
                        name=name,
                        role=role,
                        instance_id=inst["InstanceId"],
                        state=state,
                        public_ip=inst.get("PublicIpAddress"),
                        private_ip=inst.get("PrivateIpAddress"),
                    )
                    # After a Node Replacement the old, terminated instance stays visible for
                    # a while: prefer a live instance, then the most recently launched one.
                    launched = inst.get("LaunchTime")
                    rank = (
                        0 if state is InstanceState.TERMINATED else 1,
                        launched.timestamp() if launched is not None else 0.0,
                    )
                    if name not in best or rank > best[name][0]:
                        best[name] = (rank, info)
        return [info for _, info in sorted(best.values(), key=lambda x: x[1].name)]

    async def get_credentials(self, credentials_ref: str) -> Credentials:
        return await asyncio.to_thread(self._get_credentials_sync, credentials_ref)

    def _get_credentials_sync(self, credentials_ref: str) -> Credentials:
        sm = self._client("secretsmanager", self.cfg.region)
        secret = json.loads(sm.get_secret_value(SecretId=credentials_ref)["SecretString"])
        return Credentials(username=str(secret["username"]), password=str(secret["password"]))


# --- Restarting Scylla without SSH (used by ManagerClusterOps.rolling_restart) -------------


class SsmScyllaRestarter:
    """Restarts ``scylla-server`` on a Data Node through SSM Run Command (ADR-0004: no SSH)."""

    def __init__(
        self,
        region: str,
        *,
        client_factory: Callable[[str, str], Any] = _boto3_client,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.region = region
        self._client = client_factory
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    async def __call__(self, node: NodeInfo) -> None:
        ssm = self._client("ssm", self.region)
        resp = await asyncio.to_thread(
            ssm.send_command,
            InstanceIds=[node.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": ["systemctl restart scylla-server"]},
            Comment=f"dbaas rolling restart {node.name}",
        )
        command_id = resp["Command"]["CommandId"]
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                inv = await asyncio.to_thread(
                    ssm.get_command_invocation, CommandId=command_id, InstanceId=node.instance_id
                )
            except Exception as e:  # InvocationDoesNotExist right after send_command
                if "InvocationDoesNotExist" not in str(e):
                    raise
                inv = {"Status": "Pending"}
            status = inv.get("Status")
            if status == "Success":
                return
            if status in ("Cancelled", "TimedOut", "Failed", "Cancelling"):
                raise RuntimeError(
                    f"SSM restart of {node.name} {status}: {inv.get('StandardErrorContent', '')}"
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"SSM restart of {node.name} did not finish")


def _manager_ops(settings: Settings, infra: AwsInfrastructure) -> ClusterOps:
    """Build the shared ``ManagerClusterOps`` (owned by the scylla-ops agent).

    Its constructor is not part of the Provider contract, so only the keyword arguments it
    declares are passed from the candidates below.
    """
    # Lazy and untyped on purpose: the module is owned by another agent and optional here.
    manager_cluster_ops: Any = importlib.import_module("dbaas.scylla.ops").ManagerClusterOps

    restarter = SsmScyllaRestarter(settings.aws_region)
    candidates: dict[str, object] = {
        "settings": settings,
        "restart": restarter,
        "restart_node": restarter,
        "restarter": restarter,
        "get_credentials": infra.get_credentials,
        "credentials": infra.get_credentials,
        "infra": infra,
    }
    params = inspect.signature(manager_cluster_ops).parameters
    kwargs = {k: v for k, v in candidates.items() if k in params}
    ops: ClusterOps = manager_cluster_ops(**kwargs)
    return ops


def build(settings: Settings, ops: ClusterOps | None = None) -> Provider:
    infra = AwsInfrastructure(AwsConfig.from_settings(settings))
    return Provider("aws", infra, ops if ops is not None else _manager_ops(settings, infra))
