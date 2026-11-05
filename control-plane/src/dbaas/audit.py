from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from dbaas.domain import AuditResult
from dbaas.models import AuditEvent

log = structlog.get_logger("audit")


def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str | None,
    result: AuditResult,
    request_id: str | None = None,
    **details: Any,
) -> AuditEvent:
    """Add an Audit Event to the session (committed with the caller's transaction)
    and mirror it to the structured log stream."""
    ev = AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        request_id=request_id,
        details=details,
    )
    session.add(ev)
    log.info(
        "audit",
        actor=actor,
        action=action,
        target=f"{target_type}/{target_id}",
        result=str(result),
        request_id=request_id,
        **details,
    )
    return ev
