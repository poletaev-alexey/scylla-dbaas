"""Domain vocabulary. Names follow GLOSSARY.md exactly."""

from enum import StrEnum


class ClusterStatus(StrEnum):
    PROVISIONING = "provisioning"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


class NodeRole(StrEnum):
    DATA = "data"
    MONITOR = "monitor"


class NodeStatus(StrEnum):
    PENDING = "pending"
    UP = "up"
    DOWN = "down"
    REPLACING = "replacing"
    TERMINATED = "terminated"


class OperationType(StrEnum):
    CREATE_CLUSTER = "create_cluster"
    DELETE_CLUSTER = "delete_cluster"
    REPLACE_NODE = "replace_node"
    RECREATE_MONITOR = "recreate_monitor"
    ROLLING_RESTART = "rolling_restart"
    BACKUP = "backup"
    DELETE_BACKUPS = "delete_backups"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in (OperationStatus.PENDING, OperationStatus.RUNNING)


class BackupStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DELETED = "deleted"


class AuditResult(StrEnum):
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


SYSTEM_ACTOR = "system"
"""Actor used when the Control Plane acts on its own (Auto-recovery, schedules)."""

DATA_NODE_COUNT = 3

# Operations that Auto-recovery is allowed to pre-empt (ADR-0003, grilling round 4).
PREEMPTIBLE_BY_RECOVERY = frozenset({OperationType.BACKUP})
