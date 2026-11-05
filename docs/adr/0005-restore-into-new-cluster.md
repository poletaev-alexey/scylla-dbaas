# Restore creates a new Cluster from a Backup

The primary restore path is "Create Cluster from Backup": a fresh Cluster is provisioned and Scylla Manager restores schema, then data, into it. Scylla Manager can only restore schema into an empty cluster, and restoring into a new Cluster never risks the data of a live one. Consequence: Backups outlive their Cluster and are deleted only by an explicit call. In-place restore of table data into an existing Cluster is a stretch goal.
