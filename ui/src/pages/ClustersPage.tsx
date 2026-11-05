import { useState } from "react";
import { Link } from "react-router-dom";
import { useClusters } from "../api/queries";
import { CreateClusterDialog } from "../components/CreateClusterDialog";
import { Empty, ErrorNotice, Loading, Time } from "../components/misc";
import { StatusBadge } from "../components/StatusBadge";

export function ClustersPage() {
  const clusters = useClusters();
  const [creating, setCreating] = useState(false);

  return (
    <>
      <div className="page-head">
        <h1>Clusters</h1>
        <button
          type="button"
          className="primary"
          onClick={() => {
            setCreating(true);
          }}
        >
          Create Cluster
        </button>
      </div>
      <ErrorNotice error={clusters.error} />
      {clusters.isPending ? (
        <Loading />
      ) : clusters.data?.length === 0 ? (
        <Empty>No Clusters yet. Create one to get started.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Data Nodes up</th>
                <th>Auto-recovery</th>
                <th>Source</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {clusters.data?.map((c) => {
                const data = c.nodes.filter((n) => n.role === "data");
                return (
                  <tr key={c.id}>
                    <td>
                      <Link to={`/clusters/${c.id}`}>
                        <strong>{c.name}</strong>
                      </Link>
                      <div className="muted small-text">
                        <code>{c.id}</code>
                      </div>
                    </td>
                    <td>
                      <StatusBadge status={c.status} />
                    </td>
                    <td>
                      {data.length
                        ? `${data.filter((n) => n.status === "up").length} / ${data.length}`
                        : "—"}
                    </td>
                    <td>{c.auto_recovery ? "On" : "Off"}</td>
                    <td className="small-text">
                      {c.source_backup_id ? `Backup ${c.source_backup_id}` : "Empty"}
                    </td>
                    <td>
                      <Time value={c.created_at} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {creating && (
        <CreateClusterDialog
          onClose={() => {
            setCreating(false);
          }}
        />
      )}
    </>
  );
}
