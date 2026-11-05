import { useState } from "react";
import { Link } from "react-router-dom";
import { api, unwrap, type Backup } from "../api/client";
import { useBackups, useClusters, useOperationMutation } from "../api/queries";
import { CreateClusterDialog } from "../components/CreateClusterDialog";
import { Dialog } from "../components/Dialog";
import { Empty, ErrorNotice, Loading, OperationStarted, Time } from "../components/misc";
import { StatusBadge } from "../components/StatusBadge";

/** All Backups, including those whose Cluster was deleted (ADR-0005: Backups outlive Clusters). */
export function BackupsPage() {
  const backups = useBackups();
  const clusters = useClusters();
  const live = new Set((clusters.data ?? []).filter((c) => c.status !== "deleted").map((c) => c.id));
  const [restoreFrom, setRestoreFrom] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Backup | null>(null);

  return (
    <>
      <div className="page-head">
        <h1>Backups</h1>
      </div>
      <p className="muted" style={{ marginTop: 0 }}>
        Backups outlive the Cluster they came from. Restore always creates a new Cluster.
      </p>
      <ErrorNotice error={backups.error} />
      {backups.isPending ? (
        <Loading />
      ) : !backups.data?.length ? (
        <Empty>No Backups yet. Use “Backup now” on a Cluster or wait for its Backup Schedule.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Cluster</th>
                <th>Status</th>
                <th>Taken</th>
                <th>Trigger</th>
                <th>Snapshot / location</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {backups.data.map((b) => (
                <tr key={b.id}>
                  <td>
                    {live.has(b.cluster_id) ? (
                      <Link to={`/clusters/${b.cluster_id}`}>{b.cluster_name}</Link>
                    ) : (
                      <>
                        {b.cluster_name} <span className="muted small-text">(Cluster deleted)</span>
                      </>
                    )}
                  </td>
                  <td>
                    <StatusBadge status={b.status} />
                  </td>
                  <td>
                    <Time value={b.created_at} />
                  </td>
                  <td>{b.scheduled ? "Schedule" : "Manual"}</td>
                  <td className="small-text">
                    <code>{b.snapshot_tag ?? "—"}</code>
                    <div className="muted">
                      <code>{b.location}</code>
                    </div>
                  </td>
                  <td>
                    <div className="row" style={{ flexWrap: "nowrap" }}>
                      <button
                        type="button"
                        className="small"
                        disabled={b.status !== "done"}
                        onClick={() => {
                          setRestoreFrom(b.id);
                        }}
                      >
                        Restore into new Cluster
                      </button>
                      {!live.has(b.cluster_id) && (
                        <button
                          type="button"
                          className="small danger"
                          onClick={() => {
                            setDeleting(b);
                          }}
                        >
                          Delete all…
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {restoreFrom && (
        <CreateClusterDialog
          initialBackupId={restoreFrom}
          onClose={() => {
            setRestoreFrom(null);
          }}
        />
      )}
      {deleting && (
        <DeleteBackupsDialog
          backup={deleting}
          onClose={() => {
            setDeleting(null);
          }}
        />
      )}
    </>
  );
}

function DeleteBackupsDialog({ backup, onClose }: { backup: Backup; onClose: () => void }) {
  const del = useOperationMutation(async () =>
    unwrap(
      await api.DELETE("/api/v1/clusters/{cluster_id}/backups", {
        params: { path: { cluster_id: backup.cluster_id } },
      }),
    ),
  );
  return (
    <Dialog title={`Delete Backups of ${backup.cluster_name}`} onClose={onClose}>
      <div className="form">
        <p style={{ margin: 0 }}>
          Permanently delete <strong>every</strong> Backup of the deleted Cluster{" "}
          <code>{backup.cluster_name}</code> from object storage. This cannot be undone.
        </p>
        {del.data && <OperationStarted id={del.data.operation.id} type={del.data.operation.type} />}
        <ErrorNotice error={del.error} />
        <div className="form-actions">
          <button type="button" onClick={onClose}>
            {del.data ? "Close" : "Cancel"}
          </button>
          {!del.data && (
            <button
              type="button"
              className="danger"
              disabled={del.isPending}
              onClick={() => {
                del.mutate();
              }}
            >
              {del.isPending ? "Submitting…" : "Delete all Backups"}
            </button>
          )}
        </div>
      </div>
    </Dialog>
  );
}
