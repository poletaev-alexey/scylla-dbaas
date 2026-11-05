from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dbaas import audit
from dbaas.config import Settings
from dbaas.domain import AuditResult
from dbaas.operations.runner import OperationRunner


def settings(request: Request) -> Settings:
    s: Settings = request.app.state.settings
    return s


def runner(request: Request) -> OperationRunner:
    r: OperationRunner = request.app.state.runner
    return r


async def session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.sessions() as s:
        yield s


def request_id(request: Request) -> str:
    rid: str = request.state.request_id
    return rid


async def actor(
    request: Request,
    s: Annotated[AsyncSession, Depends(session)],
    cfg: Annotated[Settings, Depends(settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """API key -> Actor. Denied attempts are themselves Audit Events."""
    name = cfg.actor_for_key(x_api_key) if x_api_key else None
    if name is None:
        audit.record(
            s,
            actor="anonymous",
            action="auth.denied",
            target_type="api",
            target_id=None,
            result=AuditResult.DENIED,
            request_id=request.state.request_id,
            path=request.url.path,
        )
        await s.commit()
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")
    return name


Session = Annotated[AsyncSession, Depends(session)]
Actor = Annotated[str, Depends(actor)]
Runner = Annotated[OperationRunner, Depends(runner)]
RequestId = Annotated[str, Depends(request_id)]
Cfg = Annotated[Settings, Depends(settings)]
