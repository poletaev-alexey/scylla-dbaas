# Scylla DBaaS

A simplified Database-as-a-Service that lets a user create and operate ScyllaDB clusters on AWS through a web UI and an API.

## Language

### Topology

**Cluster**:
A managed ScyllaDB database made of three Data Nodes plus one Monitor Node, owned and operated by the Control Plane.
_Avoid_: Deployment, database, instance

**Data Node**:
One VM in a Cluster that runs ScyllaDB and stores data.
_Avoid_: Scylla node, server, instance, host

**Monitor Node**:
The dedicated VM of a Cluster that runs Scylla Manager and the Scylla Monitoring Stack; it stores no data.
_Avoid_: Manager VM, ops VM, monitoring server

**Control Plane**:
The DBaaS service (API, UI and background workers) that creates, observes and changes Clusters on the user's behalf.
_Avoid_: Backend, orchestrator, management server

### Cluster status

**Healthy**:
A Cluster whose three Data Nodes are all up and serving and whose Monitor Node is reachable.

**Degraded**:
A Cluster that still serves data at QUORUM but has at least one unavailable Data Node or an unreachable Monitor Node.
_Avoid_: Unhealthy, partial outage

**Recovering**:
A Cluster on which a Node Replacement or Monitor Node recreation is in progress.
_Avoid_: Healing, repairing (Repair is a distinct Scylla Manager concept)

**Failed**:
A Cluster whose creation did not complete and which can only be deleted.
_Avoid_: Error, broken, down

### Operations

**Operation**:
A single user- or system-initiated change to a Cluster (create, delete, rolling restart, node replacement, backup, restore) tracked from request to final outcome.
_Avoid_: Job, task, action, workflow

**Actor**:
The identity on whose behalf an Operation is performed: an API user, or the Control Plane itself for automatic actions.
_Avoid_: User (when the system acts), caller, principal

**Audit Event**:
An immutable record that an Actor attempted something important against a Cluster or the Control Plane, and with what result.
_Avoid_: Log line, history entry, activity

### Resilience

**Node Replacement**:
Swapping a failed Data Node for a fresh VM that takes over its identity and data ranges in the Cluster.
_Avoid_: Node restart, rebuild, re-provision

**Auto-recovery**:
The Control Plane's policy of starting a Node Replacement (or Monitor Node recreation) on its own when it judges a node permanently failed.
_Avoid_: Self-healing, failover

### Data protection

**Backup**:
A point-in-time copy of a Cluster's schema and data stored in object storage, which outlives the Cluster it came from.
_Avoid_: Snapshot (that is a node-local Scylla concept), dump, export

**Backup Schedule**:
The recurring policy (frequency and retention) by which Backups of a Cluster are taken automatically.
_Avoid_: Backup job, cron, backup task

**Restore**:
Creating a new Cluster whose schema and data come from an existing Backup.
_Avoid_: Recovery (reserved for Auto-recovery), rollback, import
