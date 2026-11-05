"""Public API shapes (/api/v1). The UI uses only these."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dbaas.domain import (
    AuditResult,
    BackupStatus,
    ClusterStatus,
    NodeRole,
    NodeStatus,
    OperationStatus,
    OperationType,
)

NAME_PATTERN = r"^[a-z][a-z0-9-]{2,30}$"


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ClusterCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, examples=["demo"])
    from_backup_id: str | None = Field(
        default=None, description="Restore: create the Cluster from this Backup"
    )
    auto_recovery: bool = True


class NodeOut(_Out):
    name: str
    role: NodeRole
    status: NodeStatus
    instance_id: str | None
    public_ip: str | None
    private_ip: str | None
    down_since: datetime | None


class ClusterOut(_Out):
    id: str
    name: str
    status: ClusterStatus
    auto_recovery: bool
    source_backup_id: str | None
    grafana_url: str | None
    manager_url: str | None
    backup_interval_hours: int
    backup_retention: int
    created_at: datetime
    updated_at: datetime
    nodes: list[NodeOut]


class ClusterPatch(BaseModel):
    auto_recovery: bool | None = None


class ConnectionInfo(BaseModel):
    contact_points: list[str]
    port: int = 9042
    username: str
    password: str | None = Field(description="Returned only on the first call; null afterwards")
    cql_client_command: str


class BackupScheduleIn(BaseModel):
    interval_hours: int = Field(ge=1, le=168)
    retention: int = Field(ge=1, le=90)


class BackupScheduleOut(BackupScheduleIn):
    pass


class BackupOut(_Out):
    id: str
    cluster_id: str
    cluster_name: str
    status: BackupStatus
    location: str
    snapshot_tag: str | None
    scheduled: bool
    created_at: datetime
    finished_at: datetime | None


class OperationLogOut(_Out):
    ts: datetime
    message: str


class OperationOut(_Out):
    id: str
    cluster_id: str | None
    type: OperationType
    status: OperationStatus
    actor: str
    params: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class OperationDetail(OperationOut):
    log: list[OperationLogOut]


class AuditEventOut(_Out):
    id: int
    ts: datetime
    actor: str
    action: str
    target_type: str
    target_id: str | None
    result: AuditResult
    request_id: str | None
    details: dict[str, Any]


class Accepted(BaseModel):
    """Response for every mutating call: poll the Operation to follow progress."""

    operation: OperationOut
    cluster: ClusterOut | None = None


class Problem(BaseModel):
    detail: str
    active_operation_id: str | None = None
