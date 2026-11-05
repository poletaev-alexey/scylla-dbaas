"""Tests for the ``aws`` Provider driver without touching AWS.

* Terraform is replaced by a fake ``terraform`` script placed first on PATH; it records its
  argv, TF_DATA_DIR and the tfvars it was given, and answers ``output -json`` from a fixture.
* EC2 and Secrets Manager calls run against moto.
"""

import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("boto3")
pytest.importorskip("moto")

import boto3
from moto import mock_aws

from dbaas.config import Settings
from dbaas.domain import NodeRole
from dbaas.providers.aws import (
    AwsConfig,
    AwsInfrastructure,
    TerraformError,
    ThrottledLog,
    build,
)
from dbaas.providers.base import ClusterInfra, InstanceState
from dbaas.providers.fake import FakeClusterOps, FakeWorld

REGION = "eu-central-1"

FAKE_TERRAFORM = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
sub = next(a for a in args if not a.startswith("-"))
var_file = next((a.split("=", 1)[1] for a in args if a.startswith("-var-file=")), None)
record = {{
    "args": args,
    "tf_data_dir": os.environ.get("TF_DATA_DIR"),
    "tfvars": json.load(open(var_file)) if var_file else None,
}}
with open(os.environ["FAKE_TF_LOG"], "a") as f:
    f.write(json.dumps(record) + "\n")
if os.environ.get("FAKE_TF_FAIL") == sub:
    print("Error: simulated failure in " + sub)
    sys.exit(1)
if sub == "output":
    sys.stdout.write(open(os.environ["FAKE_TF_OUTPUTS"]).read())
else:
    for i in range(20):
        print(f"aws_instance.data[\"data-1\"]: Still creating... [{{i}}0s elapsed]")
    print(f"{{sub.capitalize()}} complete! Resources: 1 added, 0 changed, 0 destroyed.")
"""


def _node(name: str, role: str, n: int, iid: str | None = None) -> dict[str, str]:
    return {
        "name": name,
        "role": role,
        "instance_id": iid or f"i-{n:017x}",
        "public_ip": f"198.51.100.{n}",
        "private_ip": f"10.42.0.{n}",
    }


def _outputs(data2_id: str = "i-00000000000000002") -> dict[str, Any]:
    nodes = {
        "data-1": _node("data-1", "data", 1),
        "data-2": _node("data-2", "data", 2, data2_id),
        "data-3": _node("data-3", "data", 3),
        "monitor": _node("monitor", "monitor", 10),
    }
    values = {
        "nodes": nodes,
        "grafana_url": "http://198.51.100.10:3000",
        "manager_url": "http://198.51.100.10:5080",
        "secret_arn": "arn:aws:secretsmanager:eu-central-1:123:secret:dbaas/c-1/cql-abc",
        "backup_location": "s3:dbaas-backups-123/clusters/c-1",
        "manager_backup_location": "s3:dbaas-backups-123",
    }
    return {k: {"sensitive": False, "type": "x", "value": v} for k, v in values.items()}


class FakeTf:
    def __init__(self, tmp: Path) -> None:
        self.log_path = tmp / "tf-calls.jsonl"
        self.outputs_path = tmp / "tf-outputs.json"
        self.set_outputs(_outputs())

    def set_outputs(self, outputs: dict[str, Any]) -> None:
        self.outputs_path.write_text(json.dumps(outputs))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def subcommands(self) -> list[str]:
        return [next(a for a in c["args"] if not a.startswith("-")) for c in self.calls()]


@pytest.fixture
def fake_tf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTf:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "terraform"
    script.write_text(FAKE_TERRAFORM.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    tf = FakeTf(tmp_path)
    monkeypatch.setenv("FAKE_TF_LOG", str(tf.log_path))
    monkeypatch.setenv("FAKE_TF_OUTPUTS", str(tf.outputs_path))
    return tf


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, msg: str) -> None:
        self.lines.append(msg)


def _config(tmp_path: Path, **kw: Any) -> AwsConfig:
    defaults: dict[str, Any] = {
        "region": REGION,
        "operator_cidr": "203.0.113.7/32",
        "module_dir": tmp_path / "module",
        "state_bucket": "dbaas-tfstate-123",
        "backup_bucket": "dbaas-backups-123",
        "work_root": tmp_path / "work",
        "ready_poll_seconds": 0.0,
        "log_interval_seconds": 3600.0,
    }
    defaults.update(kw)
    return AwsConfig(**defaults)


async def _always_open(host: str, port: int) -> bool:
    return True


def _infra(tmp_path: Path, **kw: Any) -> AwsInfrastructure:
    probe = kw.pop("port_probe", _always_open)
    lookup = kw.pop("host_id_lookup", None)
    if lookup is None:

        async def lookup(peers: Any, dead_ip: str) -> str:
            return "675ed9f4-6564-6dbd-ca08-43fddce952de"

    return AwsInfrastructure(_config(tmp_path, **kw), port_probe=probe, host_id_lookup=lookup)


# --- terraform invocation -------------------------------------------------------------------


async def test_provision_runs_init_apply_output_per_cluster(
    tmp_path: Path, fake_tf: FakeTf
) -> None:
    infra = _infra(tmp_path)
    log = Recorder()

    result = await infra.provision("c-1", "demo", log)

    assert fake_tf.subcommands() == ["init", "apply", "output"]
    init, apply, _ = fake_tf.calls()
    assert f"-chdir={tmp_path / 'module'}" in init["args"]
    assert "-backend-config=key=clusters/c-1.tfstate" in init["args"]
    assert "-backend-config=bucket=dbaas-tfstate-123" in init["args"]
    assert "-backend-config=region=eu-central-1" in init["args"]
    assert all(
        c["tf_data_dir"] == str(tmp_path / "work" / "c-1" / ".terraform") for c in fake_tf.calls()
    )
    assert "-auto-approve" in apply["args"]
    assert apply["tfvars"] == {
        "region": REGION,
        "name_prefix": "dbaas",
        "cluster_id": "c-1",
        "cluster_name": "demo",
        "operator_cidr": "203.0.113.7/32",
        "backup_bucket": "dbaas-backups-123",
        "replace_node_host_ids": {},
    }

    assert [n.name for n in result.data_nodes] == ["data-1", "data-2", "data-3"]
    assert result.monitor.role is NodeRole.MONITOR
    assert result.monitor.public_ip == "198.51.100.10"
    assert result.credentials_ref.endswith("dbaas/c-1/cql-abc")
    assert result.backup_location == "s3:dbaas-backups-123/clusters/c-1"
    assert result.grafana_url == "http://198.51.100.10:3000"
    # Terraform output reached the Operation log, throttled: 21 apply lines -> a handful.
    tf_lines = [line for line in log.lines if line.startswith("terraform: ")]
    assert any("Apply complete!" in line for line in tf_lines)
    assert len(tf_lines) < 10


async def test_clusters_get_separate_working_dirs(tmp_path: Path, fake_tf: FakeTf) -> None:
    infra = _infra(tmp_path)
    await infra.provision("c-1", "one", Recorder())
    await infra.provision("c-2", "two", Recorder())
    dirs = {c["tf_data_dir"] for c in fake_tf.calls()}
    assert dirs == {
        str(tmp_path / "work" / "c-1" / ".terraform"),
        str(tmp_path / "work" / "c-2" / ".terraform"),
    }


async def test_replace_node_passes_host_id_and_replace_flag(
    tmp_path: Path, fake_tf: FakeTf
) -> None:
    seen: dict[str, Any] = {}

    async def lookup(peers: Any, dead_ip: str) -> str:
        seen["peers"], seen["dead_ip"] = list(peers), dead_ip
        # Simulate the new instance once the lookup is done (as terraform would create it).
        fake_tf.set_outputs(_outputs(data2_id="i-0000000000000beef"))
        return "host-id-of-data-2"

    infra = _infra(tmp_path, host_id_lookup=lookup)
    await infra.provision("c-1", "demo", Recorder())
    fake_tf.log_path.unlink()

    new = await infra.replace_node("c-1", "data-2", Recorder())

    assert seen == {"peers": ["198.51.100.1", "198.51.100.3"], "dead_ip": "10.42.0.2"}
    apply = next(c for c in fake_tf.calls() if "apply" in c["args"])
    assert '-replace=aws_instance.data["data-2"]' in apply["args"]
    assert apply["tfvars"]["replace_node_host_ids"] == {"data-2": "host-id-of-data-2"}
    assert apply["tfvars"]["cluster_name"] == "demo"  # kept from provision()
    # The replace map is not left behind for later applies.
    tfvars = json.loads((tmp_path / "work" / "c-1" / "cluster.tfvars.json").read_text())
    assert tfvars["replace_node_host_ids"] == {}
    assert new.name == "data-2"
    assert new.instance_id == "i-0000000000000beef"
    assert new.private_ip == "10.42.0.2"


async def test_replace_node_rejects_monitor(tmp_path: Path, fake_tf: FakeTf) -> None:
    with pytest.raises(ValueError):
        await _infra(tmp_path).replace_node("c-1", "monitor", Recorder())
    assert fake_tf.calls() == []


async def test_recreate_monitor_replaces_only_monitor(tmp_path: Path, fake_tf: FakeTf) -> None:
    result = await _infra(tmp_path).recreate_monitor("c-1", Recorder())
    apply = next(c for c in fake_tf.calls() if "apply" in c["args"])
    assert "-replace=aws_instance.monitor" in apply["args"]
    assert apply["tfvars"]["cluster_name"] == "c-1"  # no prior provision in this work dir
    assert result.monitor.role is NodeRole.MONITOR
    assert result.manager_url == "http://198.51.100.10:5080"
    assert len(result.data_nodes) == 3


async def test_destroy_runs_destroy_and_removes_work_dir(tmp_path: Path, fake_tf: FakeTf) -> None:
    infra = _infra(tmp_path)
    await infra.provision("c-1", "demo", Recorder())
    await infra.destroy("c-1", Recorder())
    assert fake_tf.subcommands()[-2:] == ["init", "destroy"]
    destroy = fake_tf.calls()[-1]
    assert "-auto-approve" in destroy["args"]
    assert not (tmp_path / "work" / "c-1").exists()
    # Idempotent: a second destroy (no local work dir any more) still succeeds.
    await infra.destroy("c-1", Recorder())


async def test_terraform_failure_raises_with_tail(
    tmp_path: Path, fake_tf: FakeTf, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_FAIL", "apply")
    log = Recorder()
    with pytest.raises(TerraformError) as exc:
        await _infra(tmp_path).provision("c-1", "demo", log)
    assert exc.value.code == 1
    assert "Error: simulated failure in apply" in exc.value.tail
    assert any("simulated failure" in line for line in log.lines)


async def test_provision_times_out_when_nodes_never_come_up(
    tmp_path: Path, fake_tf: FakeTf
) -> None:
    async def closed(host: str, port: int) -> bool:
        return port != 9042 or host != "198.51.100.3"

    infra = _infra(tmp_path, port_probe=closed, ready_timeout_seconds=0.0)
    with pytest.raises(TimeoutError, match="data-3:9042"):
        await infra.provision("c-1", "demo", Recorder())


def test_work_dir_rejects_path_traversal(tmp_path: Path) -> None:
    infra = _infra(tmp_path)
    for bad in ("", "../x", ".hidden", "a/b"):
        with pytest.raises(ValueError):
            infra.work_dir(bad)


async def test_throttled_log_lets_important_lines_through() -> None:
    log = Recorder()
    t = ThrottledLog(log, interval=3600.0)
    await t.line("first line")
    await t.line("noise 1")
    await t.line("noise 2")
    await t.line("aws_instance.monitor: Creation complete after 31s")
    await t.line("   ")
    assert log.lines == [
        "terraform: first line",
        "terraform: aws_instance.monitor: Creation complete after 31s (+2 lines)",
    ]


# --- boto3: describe / get_credentials (moto) -----------------------------------------------


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield


def _run(ec2: Any, ami: str, cluster_id: str, node: str) -> str:
    r = ec2.run_instances(
        ImageId=ami,
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.micro",
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "dbaas:cluster_id", "Value": cluster_id},
                    {"Key": "dbaas:node", "Value": node},
                ],
            }
        ],
    )
    iid: str = r["Instances"][0]["InstanceId"]
    return iid


async def test_describe_maps_instances_by_tag(tmp_path: Path, aws: None) -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    ids = {n: _run(ec2, ami, "c-1", n) for n in ("data-1", "data-2", "data-3", "monitor")}
    _run(ec2, ami, "c-other", "data-1")  # another Cluster: must be ignored
    ec2.stop_instances(InstanceIds=[ids["data-3"]])
    # Node Replacement of data-2: old instance terminated, new one running.
    ec2.terminate_instances(InstanceIds=[ids["data-2"]])
    new_data2 = _run(ec2, ami, "c-1", "data-2")

    nodes = {n.name: n for n in await _infra(tmp_path).describe("c-1")}

    assert sorted(nodes) == ["data-1", "data-2", "data-3", "monitor"]
    assert nodes["data-1"].state is InstanceState.RUNNING
    assert nodes["data-1"].instance_id == ids["data-1"]
    assert nodes["data-1"].private_ip
    assert nodes["data-2"].instance_id == new_data2
    assert nodes["data-2"].state is InstanceState.RUNNING
    assert nodes["data-3"].state is InstanceState.STOPPED
    assert nodes["monitor"].role is NodeRole.MONITOR
    assert nodes["data-1"].role is NodeRole.DATA


async def test_describe_reports_terminated_and_unknown_cluster(tmp_path: Path, aws: None) -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    iid = _run(ec2, ami, "c-1", "data-1")
    ec2.terminate_instances(InstanceIds=[iid])
    infra = _infra(tmp_path)
    [node] = await infra.describe("c-1")
    assert node.state is InstanceState.TERMINATED
    assert await infra.describe("c-nope") == []


async def test_get_credentials_reads_secret(tmp_path: Path, aws: None) -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    arn = sm.create_secret(
        Name="dbaas/c-1/cql",
        SecretString=json.dumps(
            {"username": "scylla", "password": "s3cret", "agent_auth_token": "tok"}
        ),
    )["ARN"]
    creds = await _infra(tmp_path).get_credentials(arn)
    assert (creds.username, creds.password) == ("scylla", "s3cret")


# --- build() ---------------------------------------------------------------------------------


def test_build_combines_infra_with_given_ops(tmp_path: Path) -> None:
    settings = Settings(
        provider="aws",
        terraform_dir=str(tmp_path / "module"),
        terraform_state_bucket="st",
        backup_bucket="bk",
    )
    ops = FakeClusterOps(FakeWorld())
    p = build(settings, ops=ops)
    assert p.name == "aws"
    assert p.ops is ops
    assert isinstance(p.infra, AwsInfrastructure)
    assert p.infra.cfg.module_dir == (tmp_path / "module").resolve()
    assert p.infra.cfg.state_bucket == "st"


def test_build_wires_manager_cluster_ops(tmp_path: Path) -> None:
    from dbaas.scylla.objectstore import S3ObjectStore
    from dbaas.scylla.ops import ManagerClusterOps

    p = build(Settings(provider="aws", terraform_dir=str(tmp_path)))
    assert isinstance(p.ops, ManagerClusterOps)
    assert isinstance(p.ops._store, S3ObjectStore)
    assert p.ops._restarter is not None


async def test_secret_resolvers_for_manager(tmp_path: Path, aws: None) -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    arn = sm.create_secret(
        Name="dbaas/c-1/cql",
        SecretString=json.dumps(
            {"username": "scylla", "password": "s3cret", "agent_auth_token": "tok"}
        ),
    )["ARN"]
    view = ClusterInfra(
        nodes=[],
        grafana_url="",
        manager_url="",
        credentials_ref=arn,
        backup_location="s3:b/clusters/c-1",
    )
    infra = _infra(tmp_path)
    assert await infra.agent_auth_token(view) == "tok"
    creds = await infra.cluster_credentials(view)
    assert creds is not None and creds.password == "s3cret"
