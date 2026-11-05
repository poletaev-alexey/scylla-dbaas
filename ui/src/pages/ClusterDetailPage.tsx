import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  isActive,
  unwrap,
  type Accepted,
  type Cluster,
  type ConnectionInfo,
} from "../api/client";
import {
  keys,
  useCluster,
  useClusterBackups,
  useOperationMutation,
  useOperations,
} from "../api/queries";
import { Dialog } from "../components/Dialog";
import { CopyButton, Empty, ErrorNotice, Loading, OperationStarted, Time } from "../components/misc";
import { StatusBadge } from "../components/StatusBadge";
import { label } from "../format";

export function ClusterDetailPage() {
  const { clusterId = "" } = useParams();
  const cluster = useCluster(clusterId);

  if (cluster.isPending) return <Loading />;
  if (cluster.error instanceof ApiError && cluster.error.status === 404) {
    return (
      <Empty>
        Cluster <code>{clusterId}</code> not found (it may have been deleted).{" "}
        <Link to="/clusters">Back to Clusters</Link>
      </Empty>
    );
  }
  if (!cluster.data) return <ErrorNotice error={cluster.error} />;
  return <ClusterView c={cluster.data} />;
}

type Pending =
  | { kind: "restart" }
  | { kind: "replace"; node: string; role: string }
  | { kind: "delete" }
  | null;

function ClusterView({ c }: { c: Cluster }) {
  const ops = useOperations(c.id);
  const active = ops.data?.find((o) => isActive(o.status));
  const [confirm, setConfirm] = useState<Pending>(null);
  const [lastOp, setLastOp] = useState<Accepted | null>(null);

  const id = { params: { path: { cluster_id: c.id } } };
  const backupNow = useOperationMutation(async () =>
    unwrap(await api.POST("/api/v1/clusters/{cluster_id}/backups", id)),
  );
  const restart = useOperationMutation(async () =>
    unwrap(await api.POST("/api/v1/clusters/{cluster_id}/rolling-restart", id)),
  );
  const replace = useOperationMutation(async (node: string) =>
    unwrap(
      await api.POST("/api/v1/clusters/{cluster_id}/nodes/{node}/replace", {
        params: { path: { cluster_id: c.id, node } },
      }),
    ),
  );
  const mutations = [backupNow, restart, replace];
  // Restart/replace errors are shown inside their confirm dialog; Backup now has none.
  const lastError = backupNow.error;
  const onAccepted = (res: Accepted) => {
    setLastOp(res);
    setConfirm(null);
  };
  const resetErrors = () => {
    mutations.forEach((m) => {
      m.reset();
    });
  };

  const gone = c.status === "deleting";
  const usable = !gone && c.status !== "failed" && c.status !== "provisioning";

  return (
    <div className="stack">
      <div className="page-head">
        <h1>
          {c.name} <StatusBadge status={c.status} />
        </h1>
        <div className="row">
          <button
            type="button"
            disabled={!usable || backupNow.isPending}
            onClick={() => {
              resetErrors();
              backupNow.mutate(undefined, { onSuccess: onAccepted });
            }}
          >
            Backup now
          </button>
          <button
            type="button"
            disabled={!usable}
            onClick={() => {
              resetErrors();
              setConfirm({ kind: "restart" });
            }}
          >
            Rolling restart
          </button>
          <button
            type="button"
            className="danger"
            disabled={gone}
            onClick={() => {
              setConfirm({ kind: "delete" });
            }}
          >
            Delete…
          </button>
        </div>
      </div>

      {c.status === "failed" && (
        <div className="notice notice-danger">
          Cluster creation did not complete. A Failed Cluster can only be deleted.
        </div>
      )}
      {active && (
        <div className="notice notice-info" role="status">
          Operation in progress: <Link to={`/operations/${active.id}`}>{label(active.type)}</Link> (
          {active.status}, actor {active.actor}). Other mutating actions will be rejected until
          it finishes.
        </div>
      )}
      {lastOp && !lastError && <OperationStarted id={lastOp.operation.id} type={lastOp.operation.type} />}
      <ErrorNotice error={lastError} />

      <div className="grid-2">
        <Overview c={c} />
        <Connection c={c} />
      </div>

      <section className="card">
        <h2>Nodes</h2>
        <NodesTable
          c={c}
          disabled={!usable}
          onReplace={(node, role) => {
            resetErrors();
            setConfirm({ kind: "replace", node, role });
          }}
        />
      </section>

      <div className="grid-2">
        <ScheduleEditor c={c} />
        <ClusterBackups clusterId={c.id} />
      </div>

      <section className="card">
        <div className="page-head" style={{ marginBottom: "0.5rem" }}>
          <h2 style={{ margin: 0 }}>Recent Operations</h2>
          <Link to={`/operations?cluster_id=${c.id}`}>All Operations of this Cluster</Link>
        </div>
        {ops.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Status</th>
                <th>Actor</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {ops.data.slice(0, 8).map((o) => (
                <tr key={o.id}>
                  <td>
                    <Link to={`/operations/${o.id}`}>{label(o.type)}</Link>
                  </td>
                  <td>
                    <StatusBadge status={o.status} />
                  </td>
                  <td>{o.actor}</td>
                  <td>
                    <Time value={o.started_at ?? o.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No Operations yet.</p>
        )}
      </section>

      {confirm?.kind === "restart" && (
        <ConfirmDialog
          title="Rolling restart"
          confirmLabel="Restart"
          pending={restart.isPending}
          error={restart.error}
          onClose={() => {
            setConfirm(null);
          }}
          onConfirm={() => {
            restart.mutate(undefined, { onSuccess: onAccepted });
          }}
        >
          Restart the three Data Nodes one at a time, waiting for each to be Up/Normal again. The
          Cluster keeps serving at QUORUM throughout.
        </ConfirmDialog>
      )}
      {confirm?.kind === "replace" && (
        <ConfirmDialog
          title={confirm.role === "monitor" ? "Recreate Monitor Node" : `Replace ${confirm.node}`}
          confirmLabel={confirm.role === "monitor" ? "Recreate" : "Replace node"}
          danger
          pending={replace.isPending}
          error={replace.error}
          onClose={() => {
            setConfirm(null);
          }}
          onConfirm={() => {
            replace.mutate(confirm.node, { onSuccess: onAccepted });
          }}
        >
          {confirm.role === "monitor"
            ? "The Monitor Node will be recreated from scratch. Grafana and Scylla Manager are unavailable meanwhile; data is not affected."
            : `Node Replacement: a fresh VM takes over the identity and data ranges of ${confirm.node}. The Cluster is Recovering until it completes.`}
        </ConfirmDialog>
      )}
      {confirm?.kind === "delete" && (
        <DeleteDialog
          c={c}
          onClose={() => {
            setConfirm(null);
          }}
        />
      )}
    </div>
  );
}

function Overview({ c }: { c: Cluster }) {
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: async (enabled: boolean) =>
      unwrap(
        await api.PATCH("/api/v1/clusters/{cluster_id}", {
          params: { path: { cluster_id: c.id } },
          body: { auto_recovery: enabled },
        }),
      ),
    onSuccess: (data) => {
      qc.setQueryData(keys.cluster(c.id), data);
      void qc.invalidateQueries({ queryKey: ["audit"] });
    },
  });
  return (
    <section className="card">
      <h2>Overview</h2>
      <dl className="kv">
        <dt>ID</dt>
        <dd>
          <code>{c.id}</code>
        </dd>
        <dt>Created</dt>
        <dd>
          <Time value={c.created_at} />
        </dd>
        <dt>Restored from</dt>
        <dd>{c.source_backup_id ? <code>{c.source_backup_id}</code> : "— (empty Cluster)"}</dd>
        <dt>Grafana</dt>
        <dd>
          {c.grafana_url ? (
            <a href={c.grafana_url} target="_blank" rel="noreferrer">
              {c.grafana_url} ↗
            </a>
          ) : (
            <span className="muted">not available yet</span>
          )}
        </dd>
        <dt>Scylla Manager</dt>
        <dd>
          {c.manager_url ? (
            <a href={c.manager_url} target="_blank" rel="noreferrer">
              {c.manager_url} ↗
            </a>
          ) : (
            <span className="muted">not available yet</span>
          )}
        </dd>
        <dt>Auto-recovery</dt>
        <dd>
          <label className="inline" style={{ fontWeight: 400 }}>
            <input
              type="checkbox"
              checked={toggle.isPending ? toggle.variables : c.auto_recovery}
              disabled={toggle.isPending || c.status === "deleting"}
              onChange={(e) => {
                toggle.mutate(e.target.checked);
              }}
            />
            {c.auto_recovery ? "On" : "Off"}
            <span className="hint">— replace permanently failed nodes automatically</span>
          </label>
        </dd>
      </dl>
      <ErrorNotice error={toggle.error} />
    </section>
  );
}

function Connection({ c }: { c: Cluster }) {
  const [info, setInfo] = useState<ConnectionInfo | null>(null);
  const reveal = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.GET("/api/v1/clusters/{cluster_id}/connection", {
          params: { path: { cluster_id: c.id } },
        }),
      ),
    onSuccess: setInfo,
  });
  const command =
    info && info.password
      ? info.cql_client_command.replace("<password>", info.password)
      : (info?.cql_client_command ?? "");

  return (
    <section className="card">
      <h2>Connection</h2>
      {!info ? (
        <>
          <p className="muted small-text">
            The CQL password is shown <strong>only once</strong>, the first time connection
            info is requested. Have a place to store it ready.
          </p>
          <button
            type="button"
            disabled={reveal.isPending || c.status === "provisioning" || c.status === "failed"}
            onClick={() => {
              reveal.mutate();
            }}
          >
            Show connection info
          </button>
          <ErrorNotice error={reveal.error} />
        </>
      ) : (
        <dl className="kv">
          <dt>Contact points</dt>
          <dd>
            <code>{info.contact_points.join(", ") || "—"}</code>
          </dd>
          <dt>Port</dt>
          <dd>
            <code>{info.port}</code>
          </dd>
          <dt>Username</dt>
          <dd>
            <code>{info.username}</code>
          </dd>
          <dt>Password</dt>
          <dd>
            {info.password ? (
              <div className="stack" style={{ gap: "0.35rem" }}>
                <div className="row">
                  <code>{info.password}</code>
                  <CopyButton text={info.password} />
                </div>
                <span className="notice notice-warn small-text">
                  Copy it now: it will not be shown again.
                </span>
              </div>
            ) : (
              <span className="muted">
                Already revealed once; the Control Plane does not show it again.
              </span>
            )}
          </dd>
          <dt>cql-client</dt>
          <dd>
            <div className="stack" style={{ gap: "0.35rem" }}>
              <pre>{info.cql_client_command}</pre>
              <div>
                <CopyButton
                  text={command}
                  label={info.password ? "Copy command with password" : "Copy command"}
                />
              </div>
            </div>
          </dd>
        </dl>
      )}
    </section>
  );
}

function NodesTable({
  c,
  disabled,
  onReplace,
}: {
  c: Cluster;
  disabled: boolean;
  onReplace: (node: string, role: string) => void;
}) {
  if (!c.nodes.length) return <p className="muted">No nodes yet (provisioning).</p>;
  const nodes = [...c.nodes].sort((a, b) =>
    a.role === b.role ? a.name.localeCompare(b.name) : a.role === "data" ? -1 : 1,
  );
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>Node</th>
            <th>Role</th>
            <th>Status</th>
            <th>Instance</th>
            <th>Public IP</th>
            <th>Private IP</th>
            <th>Down since</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.name}>
              <td>
                <strong>{n.name}</strong>
              </td>
              <td>{n.role === "data" ? "Data Node" : "Monitor Node"}</td>
              <td>
                <StatusBadge status={n.status} />
              </td>
              <td>
                <code>{n.instance_id ?? "—"}</code>
              </td>
              <td>
                <code>{n.public_ip ?? "—"}</code>
              </td>
              <td>
                <code>{n.private_ip ?? "—"}</code>
              </td>
              <td>
                <Time value={n.down_since} />
              </td>
              <td>
                <button
                  type="button"
                  className="small"
                  disabled={disabled || n.status === "replacing"}
                  onClick={() => {
                    onReplace(n.name, n.role);
                  }}
                >
                  {n.role === "data" ? "Replace node" : "Recreate"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ScheduleEditor({ c }: { c: Cluster }) {
  const qc = useQueryClient();
  const [interval, setIntervalHours] = useState(String(c.backup_interval_hours));
  const [retention, setRetention] = useState(String(c.backup_retention));
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PUT("/api/v1/clusters/{cluster_id}/backup-schedule", {
          params: { path: { cluster_id: c.id } },
          body: { interval_hours: Number(interval), retention: Number(retention) },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.cluster(c.id) });
    },
  });
  const dirty =
    Number(interval) !== c.backup_interval_hours || Number(retention) !== c.backup_retention;

  return (
    <section className="card">
      <h2>Backup Schedule</h2>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="row" style={{ alignItems: "flex-start" }}>
          <label>
            Every (hours)
            <input
              type="number"
              min={1}
              max={168}
              required
              value={interval}
              onChange={(e) => {
                setIntervalHours(e.target.value);
                save.reset();
              }}
              style={{ width: "8rem" }}
            />
            <span className="hint">1–168</span>
          </label>
          <label>
            Keep (Backups)
            <input
              type="number"
              min={1}
              max={90}
              required
              value={retention}
              onChange={(e) => {
                setRetention(e.target.value);
                save.reset();
              }}
              style={{ width: "8rem" }}
            />
            <span className="hint">1–90</span>
          </label>
        </div>
        <p className="muted small-text" style={{ margin: 0 }}>
          Current: a Backup every {c.backup_interval_hours} h, keeping the last{" "}
          {c.backup_retention}.
        </p>
        <ErrorNotice error={save.error} />
        {save.isSuccess && !dirty && (
          <div className="notice notice-ok" role="status">
            Backup Schedule saved.
          </div>
        )}
        <div>
          <button
            type="submit"
            className="primary"
            disabled={!dirty || save.isPending || c.status === "deleting"}
          >
            Save schedule
          </button>
        </div>
      </form>
    </section>
  );
}

function ClusterBackups({ clusterId }: { clusterId: string }) {
  const backups = useClusterBackups(clusterId);
  return (
    <section className="card">
      <h2>Backups</h2>
      <ErrorNotice error={backups.error} />
      {backups.data?.length ? (
        <table>
          <thead>
            <tr>
              <th>Taken</th>
              <th>Status</th>
              <th>Trigger</th>
            </tr>
          </thead>
          <tbody>
            {backups.data.slice(0, 6).map((b) => (
              <tr key={b.id}>
                <td>
                  <Time value={b.created_at} />
                </td>
                <td>
                  <StatusBadge status={b.status} />
                </td>
                <td>{b.scheduled ? "Schedule" : "Manual"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No Backups yet.</p>
      )}
      <p className="small-text">
        <Link to="/backups">All Backups and Restore →</Link>
      </p>
    </section>
  );
}

function ConfirmDialog({
  title,
  confirmLabel,
  danger = false,
  pending,
  error,
  onClose,
  onConfirm,
  children,
}: {
  title: string;
  confirmLabel: string;
  danger?: boolean;
  pending: boolean;
  error: unknown;
  onClose: () => void;
  onConfirm: () => void;
  children: ReactNode;
}) {
  return (
    <Dialog title={title} onClose={onClose}>
      <div className="form">
        <p style={{ margin: 0 }}>{children}</p>
        <ErrorNotice error={error} />
        <div className="form-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={danger ? "danger" : "primary"}
            disabled={pending}
            onClick={onConfirm}
          >
            {pending ? "Submitting…" : confirmLabel}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function DeleteDialog({ c, onClose }: { c: Cluster; onClose: () => void }) {
  const navigate = useNavigate();
  const [typed, setTyped] = useState("");
  const del = useOperationMutation(async () =>
    unwrap(
      await api.DELETE("/api/v1/clusters/{cluster_id}", {
        params: { path: { cluster_id: c.id }, query: { confirm: typed } },
      }),
    ),
  );
  return (
    <Dialog title={`Delete Cluster ${c.name}`} onClose={onClose}>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          del.mutate(undefined, {
            onSuccess: (res) => {
              onClose();
              navigate(`/operations/${res.operation.id}`);
            },
          });
        }}
      >
        <p style={{ margin: 0 }}>
          This destroys all Data Nodes and the Monitor Node. Existing Backups are kept and can
          still be restored into a new Cluster.
        </p>
        <label>
          <span>
            Type <code>{c.name}</code> to confirm
          </span>
          <input
            value={typed}
            onChange={(e) => {
              setTyped(e.target.value);
            }}
            autoFocus
            autoComplete="off"
          />
        </label>
        <ErrorNotice error={del.error} />
        <div className="form-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={typed !== c.name || del.isPending}>
            {del.isPending ? "Submitting…" : "Delete Cluster"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
