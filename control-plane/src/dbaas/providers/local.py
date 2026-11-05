"""``local`` Provider driver (ADR-0006): a Cluster as Docker containers.

Per Cluster, on its own bridge network ``dbaas-<cluster_id>`` with a fixed /24 subnet:

* 3 Data Nodes: ``scylladb/scylla`` + Scylla Manager Agent (image built from
  ``deploy/local/scylla-agent/Dockerfile``, as in the Manager Docker docs), static IPs .11-.13;
* the Monitor Node as a container set: ``scylla-manager``, its Scylla backend (network alias
  ``scylla-manager-db``, which the official Manager image's config expects), Prometheus
  and Grafana (.20-.23). Grafana and the Manager API are published on ``127.0.0.1``.

One shared MinIO-compatible store (``dbaas-minio`` on network ``dbaas-s3``, persistent
volume) stands in for S3; it is attached to every Cluster network as ``minio`` and outlives
Clusters, like Backups do.

Reachability: the Control Plane and the CQL client run on the host, and Docker Desktop on
macOS does not route to bridge IPs. So every port the host needs is published on
``publish_host`` (127.0.0.1) at a deterministic port derived from the Cluster's subnet slot
(see ``_Net``). ``NodeInfo.public_ip`` carries ``host:port`` (CQL port for Data Nodes),
``private_ip`` the container IP used for inter-node traffic and by Scylla Manager.

Only the ``docker`` CLI is used (async subprocess), so the Control Plane image needs just
the CLI and the mounted Docker socket.
"""

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from dbaas.config import Settings
from dbaas.domain import DATA_NODE_COUNT, NodeRole
from dbaas.providers.base import (
    ClusterInfra,
    Credentials,
    InstanceState,
    NodeInfo,
    Provider,
    StepLog,
)
from dbaas.scylla.ops import ManagerClusterOps
from dbaas.scylla.rest import ScyllaRestClient

_REPO_ROOT = Path(__file__).resolve().parents[4]

MINIO_CONTAINER = "dbaas-minio"
MINIO_NETWORK = "dbaas-s3"
MINIO_VOLUME = "dbaas-minio-data"
LABEL_CLUSTER = "dbaas.cluster"
LABEL_NODE = "dbaas.node"
LABEL_TOKEN = "dbaas.agent_token"
MONITOR_PARTS = ("manager-db", "manager", "prometheus", "grafana")
_MONITOR_IP = {"manager-db": 20, "manager": 21, "prometheus": 22, "grafana": 23}


class LocalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DBAAS_LOCAL_", env_file=".env", extra="ignore")

    scylla_image: str = "scylladb/scylla:2026.1.15"
    agent_image: str = "scylladb/scylla-manager-agent:3.13.0"
    manager_image: str = "scylladb/scylla-manager:3.13.0"
    prometheus_image: str = "prom/prometheus:v3.5.0"
    grafana_image: str = "grafana/grafana:12.4.0"
    # MinIO no longer publishes images on Docker Hub; the Manager docs use this fork.
    minio_image: str = "pgsty/silo:RELEASE.2026-10-26T00-00-00Z"
    mc_image: str = "pgsty/mc:RELEASE.2026-10-26T00-00-00Z"
    node_image: str = "dbaas/scylla-with-agent:2026.1.15-3.13.0"
    dockerfile: str = str(_REPO_ROOT / "deploy" / "local" / "scylla-agent" / "Dockerfile")

    minio_user: str = "dbaas"
    minio_password: str = "dbaas-minio-secret"  # local-only throwaway credential
    bucket: str = "dbaas-backups"

    publish_host: str = "127.0.0.1"
    port_base: int = 21000  # Cluster slot n (1..250) uses host ports port_base + 20n + 0..19
    minio_port: int = 29000
    subnet_prefix: str = "10.213"
    smp: int = 1
    memory: str = "750M"
    node_timeout: float = 900.0
    poll_interval: float = 3.0


# --- docker CLI ---------------------------------------------------------------------------


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerResult:
    rc: int
    out: str
    err: str


class DockerRunner(Protocol):
    async def __call__(
        self, *args: str, stdin: bytes | None = None, check: bool = True
    ) -> DockerResult: ...


class DockerCli:
    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    async def __call__(
        self, *args: str, stdin: bytes | None = None, check: bool = True
    ) -> DockerResult:
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(stdin)
        res = DockerResult(proc.returncode or 0, out.decode().strip(), err.decode().strip())
        if check and res.rc != 0:
            raise DockerError(f"docker {' '.join(args[:3])}... failed: {res.err or res.out}")
        return res


_STATE = {
    "running": InstanceState.RUNNING,
    "created": InstanceState.PENDING,
    "restarting": InstanceState.PENDING,
    "paused": InstanceState.STOPPED,
    "exited": InstanceState.STOPPED,
    "dead": InstanceState.STOPPED,
    "removing": InstanceState.TERMINATED,
}


@dataclass(frozen=True)
class _Net:
    """Per-Cluster network. Its subnet's third octet is the Cluster's slot, which also fixes
    the host ports: base = port_base + 20*slot; data-i CQL base+i, REST base+10+i;
    Grafana base+4, Manager base+5, Prometheus base+6."""

    name: str
    subnet_base: str  # "10.213.42"
    agent_token: str
    port_base: int

    def ip(self, host: int) -> str:
        return f"{self.subnet_base}.{host}"

    @staticmethod
    def index(node_name: str) -> int:
        return int(node_name.rsplit("-", 1)[1])

    def data_ip(self, node_name: str) -> str:
        return self.ip(10 + self.index(node_name))

    @property
    def base(self) -> int:
        return self.port_base + 20 * int(self.subnet_base.rsplit(".", 1)[1])

    def cql_port(self, node_name: str) -> int:
        return self.base + self.index(node_name)

    def rest_port(self, node_name: str) -> int:
        return self.base + 10 + self.index(node_name)

    def monitor_port(self, part: str) -> int:
        return self.base + {"grafana": 4, "manager": 5, "prometheus": 6}[part]


def _data_names() -> list[str]:
    return [f"data-{i}" for i in range(1, DATA_NODE_COUNT + 1)]


class LocalInfrastructure:
    def __init__(
        self,
        settings: LocalSettings | None = None,
        docker: DockerRunner | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.s = settings or LocalSettings()
        self.docker: DockerRunner = docker or DockerCli()
        self._transport = transport
        self._sleep = sleep
        self._shared_lock = asyncio.Lock()  # image build + MinIO are shared by all Clusters

    # --- naming ------------------------------------------------------------------------

    @staticmethod
    def network_name(cluster_id: str) -> str:
        return f"dbaas-{cluster_id}"

    @staticmethod
    def container_name(cluster_id: str, part: str) -> str:
        return f"dbaas-{cluster_id}-{part}"

    @staticmethod
    def cluster_id_of(infra: ClusterInfra) -> str:
        return infra.credentials_ref.removeprefix("local:")

    # --- shared resources --------------------------------------------------------------

    async def _exists(self, kind: str, name: str) -> bool:
        return (await self.docker(kind, "inspect", name, check=False)).rc == 0

    async def _ensure_shared(self, log: StepLog) -> None:
        async with self._shared_lock:
            if not await self._exists("image", self.s.node_image):
                await log(f"building {self.s.node_image} (Scylla + Manager Agent)")
                dockerfile = await asyncio.to_thread(Path(self.s.dockerfile).read_bytes)
                await self.docker(
                    "build",
                    "-t",
                    self.s.node_image,
                    "--build-arg",
                    f"SCYLLA_IMAGE={self.s.scylla_image}",
                    "--build-arg",
                    f"AGENT_IMAGE={self.s.agent_image}",
                    "-",
                    stdin=dockerfile,
                )
            if not await self._exists("network", MINIO_NETWORK):
                await self.docker("network", "create", MINIO_NETWORK)
            if not await self._exists("container", MINIO_CONTAINER):
                await log("starting MinIO (local stand-in for S3)")
                await self.docker(
                    "run", "-d", "--name", MINIO_CONTAINER, "--network", MINIO_NETWORK,
                    "--restart", "unless-stopped",
                    "-p", f"{self.s.publish_host}:{self.s.minio_port}:9000",
                    "-v", f"{MINIO_VOLUME}:/data",
                    "-e", f"MINIO_ROOT_USER={self.s.minio_user}",
                    "-e", f"MINIO_ROOT_PASSWORD={self.s.minio_password}",
                    self.s.minio_image, "server", "/data",
                )  # fmt: skip
            else:
                await self.docker("start", MINIO_CONTAINER)
            for attempt in range(30):
                res = await self._mc("mb", "--ignore-existing", f"s3/{self.s.bucket}")
                if res.rc == 0:
                    break
                if attempt == 29:
                    raise DockerError(f"cannot create bucket {self.s.bucket}: {res.err}")
                await self._sleep(2)

    async def _mc(self, *args: str) -> DockerResult:
        host = f"http://{self.s.minio_user}:{self.s.minio_password}@{MINIO_CONTAINER}:9000"
        return await self.docker(
            "run", "--rm", "--network", MINIO_NETWORK, "-e", f"MC_HOST_s3={host}",
            self.s.mc_image, *args, check=False,
        )  # fmt: skip

    # --- network -----------------------------------------------------------------------

    async def _network(self, cluster_id: str) -> _Net | None:
        name = self.network_name(cluster_id)
        res = await self.docker("network", "inspect", name, "--format", "{{json .}}", check=False)
        if res.rc != 0:
            return None
        data: dict[str, Any] = json.loads(res.out)
        subnet = str(data["IPAM"]["Config"][0]["Subnet"])  # "10.213.42.0/24"
        labels: dict[str, str] = data.get("Labels") or {}
        token = labels.get(LABEL_TOKEN, "")
        return _Net(name, subnet.rsplit(".", 1)[0], token, self.s.port_base)

    async def _ensure_network(self, cluster_id: str) -> _Net:
        net = await self._network(cluster_id)
        if net is not None:
            return net
        name, token = self.network_name(cluster_id), secrets.token_hex(16)
        start = int(hashlib.sha256(cluster_id.encode()).hexdigest(), 16) % 250
        for i in range(250):
            third = (start + i) % 250 + 1
            res = await self.docker(
                "network", "create", "--driver", "bridge",
                "--subnet", f"{self.s.subnet_prefix}.{third}.0/24",
                "--label", f"{LABEL_CLUSTER}={cluster_id}",
                "--label", f"{LABEL_TOKEN}={token}",
                name, check=False,
            )  # fmt: skip
            if res.rc == 0:
                break
            if "overlap" not in res.err.lower():
                raise DockerError(f"network create {name}: {res.err}")
        else:
            raise DockerError(f"no free {self.s.subnet_prefix}.x.0/24 subnet for {name}")
        net = await self._network(cluster_id)
        assert net is not None
        return net

    async def _connect(self, network: str, container: str, *alias: str) -> None:
        args = ["network", "connect"]
        for a in alias:
            args += ["--alias", a]
        res = await self.docker(*args, network, container, check=False)
        if res.rc != 0 and "already exists" not in res.err:
            raise DockerError(f"connect {container} to {network}: {res.err}")

    # --- containers --------------------------------------------------------------------

    async def _copy_in(self, container: str, path: str, content: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix="dbaas-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(tmp, 0o644)  # read by non-root users inside the container
            await self.docker("cp", tmp, f"{container}:{path}")
        finally:
            os.unlink(tmp)

    def _agent_config(self, net: _Net) -> str:
        return (
            f"auth_token: {net.agent_token}\n"
            "scylla:\n  api_address: 127.0.0.1\n  api_port: 10000\n"
            "s3:\n"
            f"  access_key_id: {self.s.minio_user}\n"
            f"  secret_access_key: {self.s.minio_password}\n"
            "  provider: Minio\n"
            "  endpoint: http://minio:9000\n"
        )

    async def _start_data_node(
        self,
        cluster_id: str,
        net: _Net,
        node_name: str,
        seeds: list[str],
        replace_host_id: str | None = None,
    ) -> None:
        name = self.container_name(cluster_id, node_name)
        state = await self._container_state(name)
        if state is InstanceState.RUNNING:
            return
        if state is InstanceState.MISSING:
            args = [
                "create", "--name", name, "--hostname", node_name,
                "--network", net.name, "--ip", net.data_ip(node_name),
                "-p", f"{self.s.publish_host}:{net.cql_port(node_name)}:9042",
                "-p", f"{self.s.publish_host}:{net.rest_port(node_name)}:10000",
                "--label", f"{LABEL_CLUSTER}={cluster_id}", "--label", f"{LABEL_NODE}={node_name}",
                self.s.node_image,
                "--smp", str(self.s.smp), "--memory", self.s.memory, "--overprovisioned", "1",
                "--developer-mode", "1", "--cluster-name", cluster_id,
                "--seeds", ",".join(seeds), "--api-address", "0.0.0.0",
            ]  # fmt: skip
            if replace_host_id:
                args += ["--replace-node-first-boot", replace_host_id]
            await self.docker(*args)
            await self._copy_in(
                name,
                "/etc/scylla-manager-agent/scylla-manager-agent.yaml",
                self._agent_config(net),
            )
        await self.docker("start", name)

    async def _container_state(self, name: str) -> InstanceState:
        res = await self.docker(
            "container", "inspect", name, "--format", "{{.State.Status}}", check=False
        )
        if res.rc != 0:
            return InstanceState.MISSING
        return _STATE.get(res.out.strip(), InstanceState.PENDING)

    async def _wait_normal(self, address: str, what: str) -> None:
        waited = 0.0
        while True:
            try:
                async with ScyllaRestClient(address, transport=self._transport) as rest:
                    if await rest.operation_mode() == "NORMAL":
                        return
            except (httpx.HTTPError, RuntimeError, ValueError):
                pass
            if waited >= self.s.node_timeout:
                raise TimeoutError(f"{what} did not reach NORMAL within {self.s.node_timeout}s")
            await self._sleep(self.s.poll_interval)
            waited += self.s.poll_interval

    def _prometheus_config(self, net: _Net) -> str:
        data = [net.data_ip(n) for n in _data_names()]

        def job(name: str, targets: list[str]) -> dict[str, Any]:
            return {"job_name": name, "static_configs": [{"targets": targets}]}

        return json.dumps(  # JSON is valid YAML
            {
                "global": {"scrape_interval": "15s"},
                "scrape_configs": [
                    job("scylla", [f"{ip}:9180" for ip in data]),
                    job("manager_agent", [f"{ip}:5090" for ip in data]),
                    job("scylla_manager", [f"{net.ip(_MONITOR_IP['manager'])}:5090"]),
                ],
            },
            indent=2,
        )

    def _grafana_datasource(self, net: _Net) -> str:
        return json.dumps(
            {
                "apiVersion": 1,
                "datasources": [
                    {
                        "name": "prometheus",
                        "type": "prometheus",
                        "access": "proxy",
                        "url": f"http://{net.ip(22)}:9090",
                        "isDefault": True,
                    }
                ],
            },
            indent=2,
        )

    async def _start_monitor(self, cluster_id: str, net: _Net) -> None:
        def base(part: str) -> list[str]:
            return [
                "--name", self.container_name(cluster_id, part),
                "--network", net.name, "--ip", net.ip(_MONITOR_IP[part]),
                "--label", f"{LABEL_CLUSTER}={cluster_id}", "--label", f"{LABEL_NODE}=monitor",
                "--restart", "unless-stopped",
            ]  # fmt: skip

        def publish(part: str, port: int) -> list[str]:
            return ["-p", f"{self.s.publish_host}:{net.monitor_port(part)}:{port}"]

        async def ensure(part: str, create: list[str], files: dict[str, str]) -> None:
            name = self.container_name(cluster_id, part)
            if await self._container_state(name) is InstanceState.MISSING:
                await self.docker("create", *base(part), *create)
                for path, content in files.items():
                    await self._copy_in(name, path, content)
            await self.docker("start", name)

        await ensure(
            "manager-db",
            ["--network-alias", "scylla-manager-db", self.s.scylla_image,
             "--smp", "1", "--memory", "500M", "--overprovisioned", "1", "--developer-mode", "1"],
            {},
        )  # fmt: skip
        await ensure("manager", [*publish("manager", 5080), self.s.manager_image], {})
        await ensure(
            "prometheus",
            [*publish("prometheus", 9090), self.s.prometheus_image],
            {"/etc/prometheus/prometheus.yml": self._prometheus_config(net)},
        )
        await ensure(
            "grafana",
            [*publish("grafana", 3000),
             "-e", "GF_AUTH_ANONYMOUS_ENABLED=true", "-e", "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer",
             self.s.grafana_image],
            {"/etc/grafana/provisioning/datasources/dbaas.yaml": self._grafana_datasource(net)},
        )  # fmt: skip

    def _host(self, port: int) -> str:
        return f"{self.s.publish_host}:{port}"

    async def _infra(self, cluster_id: str, net: _Net) -> ClusterInfra:
        return ClusterInfra(
            nodes=await self.describe(cluster_id),
            grafana_url=f"http://{self._host(net.monitor_port('grafana'))}",
            manager_url=f"http://{self._host(net.monitor_port('manager'))}",
            credentials_ref=f"local:{cluster_id}",
            backup_location=f"s3:{self.s.bucket}/clusters/{cluster_id}",
        )

    # --- Infrastructure contract -------------------------------------------------------

    async def provision(self, cluster_id: str, name: str, log: StepLog) -> ClusterInfra:
        await self._ensure_shared(log)
        net = await self._ensure_network(cluster_id)
        await log(f"network {net.name} ({net.subnet_base}.0/24)")
        await self._connect(net.name, MINIO_CONTAINER, "minio")
        seed = net.data_ip("data-1")
        # One node at a time: the first one forms the cluster, the others join it.
        for node in _data_names():
            await log(f"starting Data Node {node} ({net.data_ip(node)})")
            await self._start_data_node(cluster_id, net, node, [seed])
            await self._wait_normal(self._host(net.rest_port(node)), node)
            await log(f"{node} is UN")
        await log("starting Monitor Node (Scylla Manager, Prometheus, Grafana)")
        await self._start_monitor(cluster_id, net)
        return await self._infra(cluster_id, net)

    async def destroy(self, cluster_id: str, log: StepLog) -> None:
        res = await self.docker(
            "ps", "-aq", "--filter", f"label={LABEL_CLUSTER}={cluster_id}", check=False
        )
        ids = res.out.split()
        if ids:
            await log(f"removing {len(ids)} containers")
            await self.docker("rm", "-f", "-v", *ids)
        name = self.network_name(cluster_id)
        if await self._exists("network", name):
            await self.docker("network", "disconnect", "-f", name, MINIO_CONTAINER, check=False)
            await self.docker("network", "rm", name)
            await log(f"removed network {name}")

    async def replace_node(self, cluster_id: str, node_name: str, log: StepLog) -> NodeInfo:
        net = await self._network(cluster_id)
        if net is None:
            raise LookupError(f"Cluster {cluster_id} has no network")
        old_ip = net.data_ip(node_name)
        others = [n for n in _data_names() if n != node_name]
        peers = [net.data_ip(n) for n in others]
        host_id = await self._host_id_of(old_ip, [self._host(net.rest_port(n)) for n in others])
        await log(f"{node_name} ({old_ip}) has host ID {host_id}; removing its container")
        await self.docker("rm", "-f", "-v", self.container_name(cluster_id, node_name), check=False)
        await log(f"starting {node_name} with --replace-node-first-boot {host_id}")
        await self._start_data_node(cluster_id, net, node_name, peers, replace_host_id=host_id)
        await self._wait_normal(self._host(net.rest_port(node_name)), node_name)
        await log(f"{node_name} replaced and UN")
        return next(n for n in await self.describe(cluster_id) if n.name == node_name)

    async def _host_id_of(self, address: str, peer_endpoints: list[str]) -> str:
        for peer in peer_endpoints:
            try:
                async with ScyllaRestClient(peer, transport=self._transport) as rest:
                    ids = await rest.host_ids()
            except (httpx.HTTPError, RuntimeError, ValueError):
                continue
            if address in ids:
                return ids[address]
        raise LookupError(f"no live peer knows the host ID of {address}")

    async def recreate_monitor(self, cluster_id: str, log: StepLog) -> ClusterInfra:
        net = await self._network(cluster_id)
        if net is None:
            raise LookupError(f"Cluster {cluster_id} has no network")
        names = [self.container_name(cluster_id, p) for p in MONITOR_PARTS]
        await log("removing Monitor Node containers")
        await self.docker("rm", "-f", "-v", *names, check=False)
        await self._start_monitor(cluster_id, net)
        await log("Monitor Node recreated")
        return await self._infra(cluster_id, net)

    async def describe(self, cluster_id: str) -> list[NodeInfo]:
        net = await self._network(cluster_id)
        if net is None:
            return []
        res = await self.docker(
            "ps", "-a", "--filter", f"label={LABEL_CLUSTER}={cluster_id}",
            "--format", "{{.Names}}\t{{.ID}}\t{{.State}}", check=False,
        )  # fmt: skip
        found: dict[str, tuple[str, InstanceState]] = {}
        for line in res.out.splitlines():
            cname, cid, state = [*line.split("\t"), "", "", ""][:3]
            found[cname] = (cid, _STATE.get(state, InstanceState.PENDING))

        nodes = []
        for n in _data_names():
            cid, state = found.get(self.container_name(cluster_id, n), ("", InstanceState.MISSING))
            nodes.append(
                NodeInfo(
                    n,
                    NodeRole.DATA,
                    cid,
                    state,
                    public_ip=self._host(net.cql_port(n)),
                    private_ip=net.data_ip(n),
                )
            )
        parts = [found.get(self.container_name(cluster_id, p)) for p in MONITOR_PARTS]
        manager = found.get(self.container_name(cluster_id, "manager"))
        if manager is None:
            mstate = InstanceState.MISSING
        elif all(p is not None and p[1] is InstanceState.RUNNING for p in parts):
            mstate = InstanceState.RUNNING
        else:
            mstate = InstanceState.STOPPED
        mip = net.ip(_MONITOR_IP["manager"])
        nodes.append(
            NodeInfo(
                "monitor",
                NodeRole.MONITOR,
                manager[0] if manager else "",
                mstate,
                # The Monitor Node exposes several ports; its URLs are in ClusterInfra.
                public_ip=self.s.publish_host,
                private_ip=mip,
            )
        )
        return nodes

    async def get_credentials(self, credentials_ref: str) -> Credentials:
        # Local Clusters run without CQL authentication: ScyllaDB has no default superuser
        # any more, and creating one needs the maintenance socket. Any credentials work.
        return Credentials("cassandra", "cassandra")

    # --- hooks for ManagerClusterOps -----------------------------------------------------

    async def agent_token(self, infra: ClusterInfra) -> str:
        net = await self._network(self.cluster_id_of(infra))
        if net is None:
            raise LookupError("Cluster network not found")
        return net.agent_token

    async def restart_node(self, infra: ClusterInfra, node: NodeInfo) -> None:
        await self.docker("restart", self.container_name(self.cluster_id_of(infra), node.name))


class DockerMcObjectStore:
    """Deletes Backup objects from the local MinIO with a throwaway ``mc`` container."""

    def __init__(self, infra: LocalInfrastructure) -> None:
        self.infra = infra

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        res = await self.infra._mc("rm", "--recursive", "--force", f"s3/{bucket}/{prefix}")
        if res.rc != 0 and "does not exist" not in res.err.lower():
            raise DockerError(f"mc rm s3/{bucket}/{prefix}: {res.err}")
        return sum(1 for line in res.out.splitlines() if line.startswith("Removed"))


def rest_endpoint(node: NodeInfo) -> str | None:
    """Host-published Scylla REST endpoint of a Data Node (``public_ip`` is host:CQL port)."""
    if not node.public_ip or ":" not in node.public_ip:
        return None
    host, port = node.public_ip.rsplit(":", 1)
    return f"{host}:{int(port) + 10}"


def build(settings: Settings) -> Provider:
    local = LocalSettings()
    infra = LocalInfrastructure(local)
    ops = ManagerClusterOps(
        auth_token=infra.agent_token,
        restarter=infra.restart_node,
        object_store=DockerMcObjectStore(infra),
        node_address=rest_endpoint,
        poll_interval=local.poll_interval,
        node_timeout=local.node_timeout,
    )
    return Provider("local", infra, ops)
