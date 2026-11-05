from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from dbaas.config import Settings
from dbaas.main import create_app
from dbaas.operations.health import HealthChecker
from dbaas.operations.runner import OperationRunner
from dbaas.providers.fake import FakeWorld, fake_provider

HEADERS = {"X-API-Key": "test-key"}


@dataclass
class Env:
    app: FastAPI
    client: httpx.AsyncClient
    world: FakeWorld

    @property
    def runner(self) -> OperationRunner:
        r: OperationRunner = self.app.state.runner
        return r

    @property
    def health(self) -> HealthChecker:
        h: HealthChecker = self.app.state.health
        return h

    async def wait(self) -> None:
        await self.runner.wait_all()

    async def create_cluster(self, name: str = "demo", **extra: object) -> str:
        r = await self.client.post("/api/v1/clusters", json={"name": name, **extra})
        assert r.status_code == 202, r.text
        await self.wait()
        cid: str = r.json()["cluster"]["id"]
        return cid


@pytest.fixture
async def env(tmp_path: object) -> AsyncIterator[Env]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",  # type: ignore[str-bytes-safe]
        api_keys="test-key:alice",
        recovery_grace_seconds=60,
    )
    provider, world = fake_provider()
    app = create_app(settings, provider, start_health_checker=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t", headers=HEADERS
        ) as client:
            yield Env(app, client, world)
