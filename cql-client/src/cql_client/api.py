"""Thin synchronous client for the Control Plane public API (/api/v1).

Only what the CQL client and the end-to-end scenario need: connection info, Cluster
lifecycle, Backups and Operation polling.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

TERMINAL_OK = "succeeded"
TERMINAL_FAILED = frozenset({"failed", "cancelled"})


class ApiError(RuntimeError):
    pass


class OperationFailed(ApiError):
    def __init__(self, operation: dict[str, Any]) -> None:
        self.operation = operation
        super().__init__(
            f"Operation {operation.get('id')} ({operation.get('type')}) "
            f"ended {operation.get('status')}: {operation.get('error')}"
        )


@dataclass(frozen=True)
class ConnectionInfo:
    contact_points: list[str]
    port: int
    username: str
    password: str | None  # the API reveals it only on the first call
    address_map: dict[str, str] = field(default_factory=dict)  # private IP -> public IP


class DbaasApi:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-API-Key": api_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DbaasApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- low level --------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self._http.request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(f"{method} {path} -> HTTP {r.status_code}: {detail}")
        return r.json()

    def _obj(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._request(method, path, **kwargs))

    # ---- Clusters ---------------------------------------------------------------------

    def cluster(self, cluster_id: str) -> dict[str, Any]:
        return self._obj("GET", f"/clusters/{cluster_id}")

    def create_cluster(self, name: str, from_backup_id: str | None = None) -> tuple[str, str]:
        """Returns (cluster_id, operation_id)."""
        body: dict[str, Any] = {"name": name}
        if from_backup_id:
            body["from_backup_id"] = from_backup_id
        acc = self._obj("POST", "/clusters", json=body)
        return str(acc["cluster"]["id"]), str(acc["operation"]["id"])

    def delete_cluster(self, cluster_id: str, name: str) -> str:
        acc = self._obj("DELETE", f"/clusters/{cluster_id}", params={"confirm": name})
        return str(acc["operation"]["id"])

    def backup_now(self, cluster_id: str) -> str:
        acc = self._obj("POST", f"/clusters/{cluster_id}/backups")
        return str(acc["operation"]["id"])

    def delete_backups(self, cluster_id: str) -> str:
        acc = self._obj("DELETE", f"/clusters/{cluster_id}/backups")
        return str(acc["operation"]["id"])

    def connection(self, cluster_id: str) -> ConnectionInfo:
        info = self._obj("GET", f"/clusters/{cluster_id}/connection")
        cluster = self.cluster(cluster_id)
        address_map = {
            n["private_ip"]: n["public_ip"]
            for n in cluster.get("nodes", [])
            if n.get("role") == "data" and n.get("private_ip") and n.get("public_ip")
        }
        return ConnectionInfo(
            contact_points=list(info["contact_points"]),
            port=int(info.get("port", 9042)),
            username=str(info["username"]),
            password=info.get("password"),
            address_map=address_map,
        )

    # ---- Operations -------------------------------------------------------------------

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._obj("GET", f"/operations/{operation_id}")

    def wait_operation(
        self,
        operation_id: str,
        *,
        timeout: float = 3600.0,
        poll: float = 5.0,
        on_log: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Poll until the Operation is terminal; stream new log lines to ``on_log``.
        Raises ``OperationFailed`` unless it succeeded, ``TimeoutError`` on timeout."""
        deadline = clock() + timeout
        seen = 0
        while True:
            op = self.operation(operation_id)
            log = op.get("log") or []
            if on_log is not None:
                for entry in log[seen:]:
                    on_log(str(entry.get("message", "")))
            seen = len(log)
            status = op.get("status")
            if status == TERMINAL_OK:
                return op
            if status in TERMINAL_FAILED:
                raise OperationFailed(op)
            if clock() >= deadline:
                raise TimeoutError(f"Operation {operation_id} still {status} after {timeout}s")
            sleep(poll)
