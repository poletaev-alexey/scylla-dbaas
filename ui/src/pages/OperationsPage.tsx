import { Link, useSearchParams } from "react-router-dom";
import { isActive } from "../api/client";
import { useClusters, useOperations } from "../api/queries";
import { Empty, ErrorNotice, Loading, Time } from "../components/misc";
import { StatusBadge } from "../components/StatusBadge";
import { label } from "../format";

export function OperationsPage() {
  const [params, setParams] = useSearchParams();
  const clusterId = params.get("cluster_id") ?? "";
  const ops = useOperations(clusterId || undefined);
  const clusters = useClusters();
  const names = new Map((clusters.data ?? []).map((c) => [c.id, c.name]));
  const anyActive = ops.data?.some((o) => isActive(o.status));

  return (
    <>
      <div className="page-head">
        <h1>Operations</h1>
        <span className="muted small-text" role="status">
          {anyActive ? "Live: refreshing every 2 s" : "Refreshing every 10 s"}
        </span>
      </div>
      <div className="filters">
        <label>
          Cluster
          <select
            value={clusterId}
            onChange={(e) => {
              setParams(e.target.value ? { cluster_id: e.target.value } : {});
            }}
          >
            <option value="">All Clusters</option>
            {(clusters.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
            {clusterId && !names.has(clusterId) && <option value={clusterId}>{clusterId}</option>}
          </select>
        </label>
      </div>
      <ErrorNotice error={ops.error} />
      {ops.isPending ? (
        <Loading />
      ) : !ops.data?.length ? (
        <Empty>No Operations.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Operation</th>
                <th>Status</th>
                <th>Cluster</th>
                <th>Actor</th>
                <th>Created</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {ops.data.map((o) => (
                <tr key={o.id}>
                  <td>
                    <Link to={`/operations/${o.id}`}>{label(o.type)}</Link>
                    <div className="muted small-text">
                      <code>{o.id}</code>
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={o.status} />
                    {o.error && (
                      <div className="small-text" style={{ color: "var(--danger)" }}>
                        {o.error}
                      </div>
                    )}
                  </td>
                  <td>
                    {o.cluster_id ? (
                      <Link to={`/clusters/${o.cluster_id}`}>
                        {names.get(o.cluster_id) ?? o.cluster_id}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{o.actor}</td>
                  <td>
                    <Time value={o.created_at} />
                  </td>
                  <td>
                    <Time value={o.finished_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
