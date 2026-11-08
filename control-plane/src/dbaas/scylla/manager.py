"""Async client for the Scylla Manager REST API (``/api/v1``).

Endpoints and payloads follow the Manager swagger spec
(https://github.com/scylladb/scylla-manager/blob/master/v3/swagger/scylla-manager.json):

* ``POST /clusters`` -> 201 + ``Location: /api/v1/cluster/{id}``; the body may carry an
  explicit ``id`` (same as ``sctool cluster add --id``).
* ``POST /cluster/{c}/tasks`` -> 201 + ``Location: .../task/{type}/{task_id}``.
* ``GET /cluster/{c}/task/{type}/{task_id}/latest`` -> ``{"run": TaskRun, "progress": ...}``;
  ``latest`` is the run-id alias handled by Manager's ``tryReadOffset``. 404 until a run exists.
* ``GET/DELETE /cluster/{c}/backups`` with ``locations``/``snapshot_tags`` query params.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx

JSON = dict[str, Any]

CLUSTER_WRITE_TIMEOUT = 180.0

# TaskRun.status values (pkg/service/scheduler/model.go).
RUN_DONE = "DONE"
RUN_FAILED = frozenset({"ERROR", "ABORTED", "STOPPED"})


class ManagerError(RuntimeError):
    """Scylla Manager returned an error or a task run did not finish successfully."""


@dataclass(frozen=True)
class TaskRef:
    cluster_id: str
    type: str
    task_id: str


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id_from_location(response: httpx.Response) -> str:
    loc = str(response.headers.get("location", ""))
    ident = loc.rstrip("/").rsplit("/", 1)[-1]
    if not ident:
        raise ManagerError(f"Manager response without Location header: {response.text}")
    return ident


class ManagerClient:
    """Thin typed wrapper; one instance per Manager (i.e. per Monitor Node)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1", timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> "ManagerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        read_timeout: float | None = None,
    ) -> httpx.Response:
        extra: dict[str, Any] = {} if read_timeout is None else {"timeout": read_timeout}
        r = await self._http.request(method, path, json=json, params=params, **extra)
        if r.status_code not in ok:
            raise ManagerError(f"{method} {path} -> {r.status_code}: {r.text.strip()[:500]}")
        return r

    # --- clusters -----------------------------------------------------------------------

    async def version(self) -> str:
        r = await self._request("GET", "/version")
        return str(r.json().get("version", ""))

    async def get_cluster(self, cluster_id: str) -> JSON | None:
        r = await self._request("GET", f"/cluster/{cluster_id}", ok=(200, 404))
        return None if r.status_code == 404 else cast(JSON, r.json())

    async def add_cluster(self, body: JSON) -> str:
        # Manager probes every node's Agent (with its own retries) before answering;
        # on AWS that took longer than the 30 s default.
        r = await self._request("POST", "/clusters", json=body, read_timeout=CLUSTER_WRITE_TIMEOUT)
        return _id_from_location(r)

    async def update_cluster(self, cluster_id: str, body: JSON) -> None:
        await self._request(
            "PUT", f"/cluster/{cluster_id}", json=body, read_timeout=CLUSTER_WRITE_TIMEOUT
        )

    async def cluster_status(self, cluster_id: str) -> list[JSON]:
        r = await self._request("GET", f"/cluster/{cluster_id}/status")
        return cast(list[JSON], r.json())

    # --- tasks --------------------------------------------------------------------------

    async def list_tasks(self, cluster_id: str, task_type: str | None = None) -> list[JSON]:
        params: dict[str, Any] = {"all": "true"}
        if task_type:
            params["type"] = task_type
        r = await self._request("GET", f"/cluster/{cluster_id}/tasks", params=params)
        return cast(list[JSON], r.json() or [])

    async def create_task(
        self,
        cluster_id: str,
        task_type: str,
        *,
        name: str,
        properties: JSON,
        schedule: JSON | None = None,
        enabled: bool = True,
    ) -> TaskRef:
        body = {
            "type": task_type,
            "name": name,
            "enabled": enabled,
            "schedule": schedule or {},
            "properties": properties,
        }
        r = await self._request("POST", f"/cluster/{cluster_id}/tasks", json=body)
        return TaskRef(cluster_id, task_type, _id_from_location(r))

    async def update_task(self, ref: TaskRef, body: JSON) -> None:
        await self._request(
            "PUT", f"/cluster/{ref.cluster_id}/task/{ref.type}/{ref.task_id}", json=body
        )

    async def start_task(self, ref: TaskRef) -> None:
        await self._request("PUT", f"/cluster/{ref.cluster_id}/task/{ref.type}/{ref.task_id}/start")

    async def latest_run(self, ref: TaskRef) -> JSON | None:
        """``{"run": {...}, "progress": {...}}`` of the latest run, or None if none yet."""
        r = await self._request(
            "GET",
            f"/cluster/{ref.cluster_id}/task/{ref.type}/{ref.task_id}/latest",
            ok=(200, 404),
        )
        if r.status_code == 404:
            return None
        data = cast(JSON, r.json())
        return data if data.get("run") else None

    async def wait_for_run(
        self,
        ref: TaskRef,
        *,
        max_wait: float,
        poll_interval: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_progress: Callable[[JSON], Awaitable[None]] | None = None,
    ) -> JSON:
        """Poll until the task's latest run is DONE; raise on ERROR/ABORTED/STOPPED/timeout."""
        waited = 0.0
        last_status = ""
        while True:
            data = await self.latest_run(ref)
            if data is not None:
                run = cast(JSON, data["run"])
                status = str(run.get("status", ""))
                if status != last_status and on_progress is not None:
                    await on_progress(data)
                last_status = status
                if status == RUN_DONE:
                    return data
                if status in RUN_FAILED:
                    cause = run.get("cause") or "no cause reported"
                    raise ManagerError(f"{ref.type} task {ref.task_id} {status}: {cause}")
            if waited >= max_wait:
                raise ManagerError(
                    f"{ref.type} task {ref.task_id} did not finish within {max_wait:.0f}s "
                    f"(last status: {last_status or 'no run yet'})"
                )
            await sleep(poll_interval)
            waited += poll_interval

    # --- backups ------------------------------------------------------------------------

    async def list_backups(
        self, cluster_id: str, locations: list[str], *, all_clusters: bool = False
    ) -> list[JSON]:
        params: dict[str, Any] = {"locations": locations, "keyspace": ["*"]}
        if not all_clusters:
            params["query_cluster_id"] = cluster_id
        r = await self._request("GET", f"/cluster/{cluster_id}/backups", params=params)
        return cast(list[JSON], r.json() or [])

    async def delete_backups(
        self, cluster_id: str, locations: list[str], snapshot_tags: list[str]
    ) -> None:
        params = {"locations": locations, "snapshot_tags": snapshot_tags}
        await self._request("DELETE", f"/cluster/{cluster_id}/backups", params=params)
