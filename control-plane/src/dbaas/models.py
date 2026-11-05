"""Persistence model of the Control Plane."""

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from dbaas.domain import (
    AuditResult,
    BackupStatus,
    ClusterStatus,
    NodeRole,
    NodeStatus,
    OperationStatus,
    OperationType,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def enum_col[E: StrEnum](enum: type[E]) -> SAEnum:
    """Store enums as their string value and load them back as enum members."""
    return SAEnum(
        enum,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSON,
    }


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("c"))
    name: Mapped[str] = mapped_column(String(63), index=True)
    status: Mapped[ClusterStatus] = mapped_column(
        enum_col(ClusterStatus), default=ClusterStatus.PROVISIONING
    )
    auto_recovery: Mapped[bool] = mapped_column(Boolean, default=True)
    source_backup_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    backup_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    backup_retention: Mapped[int] = mapped_column(Integer, default=7)

    grafana_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credentials_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backup_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_revealed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    nodes: Mapped[list["Node"]] = relationship(
        back_populates="cluster",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Node.name",
    )


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("n"))
    cluster_id: Mapped[str] = mapped_column(ForeignKey("clusters.id"), index=True)
    name: Mapped[str] = mapped_column(String(32))  # data-1..data-3, monitor
    role: Mapped[NodeRole] = mapped_column(enum_col(NodeRole))
    status: Mapped[NodeStatus] = mapped_column(enum_col(NodeStatus), default=NodeStatus.PENDING)
    instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    private_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    down_since: Mapped[datetime | None] = mapped_column(nullable=True)

    cluster: Mapped[Cluster] = relationship(back_populates="nodes")


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("op"))
    cluster_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    type: Mapped[OperationType] = mapped_column(enum_col(OperationType))
    status: Mapped[OperationStatus] = mapped_column(
        enum_col(OperationStatus), default=OperationStatus.PENDING
    )
    actor: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    log: Mapped[list["OperationLogEntry"]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="OperationLogEntry.id",
    )


class OperationLogEntry(Base):
    __tablename__ = "operation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), index=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    message: Mapped[str] = mapped_column(Text)


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("b"))
    # Plain columns, no FK: a Backup outlives its Cluster (ADR-0005).
    cluster_id: Mapped[str] = mapped_column(String(32), index=True)
    cluster_name: Mapped[str] = mapped_column(String(63))
    status: Mapped[BackupStatus] = mapped_column(
        enum_col(BackupStatus), default=BackupStatus.RUNNING
    )
    location: Mapped[str] = mapped_column(String(255))
    snapshot_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AuditEvent(Base):
    """Append-only. Never updated or deleted by application code."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    result: Mapped[AuditResult] = mapped_column(enum_col(AuditResult))
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
