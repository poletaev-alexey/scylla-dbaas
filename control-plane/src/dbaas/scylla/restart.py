"""Ways to restart ScyllaDB on one Data Node without SSH (ADR-0002).

Each factory returns a ``dbaas.scylla.ops.NodeRestarter``. The local driver has its own
``docker restart`` restarter in ``dbaas.providers.local``.
"""

import asyncio
from typing import Any

from dbaas.providers.base import ClusterInfra, NodeInfo
from dbaas.scylla.ops import NodeRestarter

RESTART_COMMAND = "systemctl restart scylla-server"
_FAILED = frozenset({"Cancelled", "TimedOut", "Failed", "Cancelling"})


def ssm_restarter(
    region: str, *, timeout: float = 900.0, poll_interval: float = 5.0
) -> NodeRestarter:
    """Run ``systemctl restart scylla-server`` through SSM ``AWS-RunShellScript``."""

    def _client() -> Any:
        import boto3  # type: ignore[import-untyped]  # lazy: optional dependency

        return boto3.client("ssm", region_name=region)

    async def restart(_: ClusterInfra, node: NodeInfo) -> None:
        ssm = _client()
        resp = await asyncio.to_thread(
            ssm.send_command,
            InstanceIds=[node.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [RESTART_COMMAND]},
            Comment=f"dbaas rolling restart of {node.name}",
        )
        command_id = resp["Command"]["CommandId"]
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            try:
                inv = await asyncio.to_thread(
                    ssm.get_command_invocation, CommandId=command_id, InstanceId=node.instance_id
                )
            except ssm.exceptions.InvocationDoesNotExist:
                continue  # SSM needs a moment before the invocation is visible
            status = inv["Status"]
            if status == "Success":
                return
            if status in _FAILED:
                err = inv.get("StandardErrorContent", "")
                raise RuntimeError(f"SSM restart of {node.name} {status}: {err}")
        raise TimeoutError(f"SSM restart of {node.name} did not finish within {timeout}s")

    return restart
