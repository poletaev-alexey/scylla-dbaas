"""Async client for the ScyllaDB REST API (port 10000).

Endpoints verified against the API docs shipped in the ScyllaDB source tree
(https://github.com/scylladb/scylladb/tree/master/api/api-doc, ``gossiper.json`` and
``storage_service.json``).
"""

from typing import Any, cast

import httpx

DEFAULT_PORT = 10000


class ScyllaRestError(RuntimeError):
    pass


class ScyllaRestClient:
    def __init__(
        self,
        address: str,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``address`` is an IPv4/hostname, optionally ``host:port`` (overrides ``port``),
        e.g. a host-published port of the local driver."""
        host, sep, explicit_port = address.rpartition(":")
        if not sep:
            host = address
        else:
            port = int(explicit_port)
        self.address = address
        self._http = httpx.AsyncClient(
            base_url=f"http://{host}:{port}", timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> "ScyllaRestClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> Any:
        r = await self._http.get(path)
        if r.status_code != 200:
            raise ScyllaRestError(f"GET {path} on {self.address} -> {r.status_code}: {r.text}")
        return r.json()

    async def live_endpoints(self) -> set[str]:
        """Addresses this node's gossiper considers alive (includes itself)."""
        return set(cast(list[str], await self._get("/gossiper/endpoint/live/")))

    async def down_endpoints(self) -> set[str]:
        return set(cast(list[str], await self._get("/gossiper/endpoint/down/")))

    async def joining_nodes(self) -> set[str]:
        return set(cast(list[str], await self._get("/storage_service/nodes/joining")))

    async def host_ids(self) -> dict[str, str]:
        """endpoint address -> host ID, for every token-owning node."""
        items = cast(list[dict[str, str]], await self._get("/storage_service/host_id"))
        return {i["key"]: i["value"] for i in items}

    async def operation_mode(self) -> str:
        """``NORMAL`` once the node has joined the ring and serves traffic."""
        return str(await self._get("/storage_service/operation_mode")).strip('"').upper()

    async def native_transport_running(self) -> bool:
        return bool(await self._get("/storage_service/native_transport"))

    async def drain(self) -> None:
        """Flush memtables and stop accepting writes; done before a restart."""
        r = await self._http.post("/storage_service/drain", timeout=300.0)
        if r.status_code != 200:
            raise ScyllaRestError(f"drain on {self.address} -> {r.status_code}: {r.text}")
