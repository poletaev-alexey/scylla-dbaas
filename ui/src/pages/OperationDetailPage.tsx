import { useEffect, useRef } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, isActive } from "../api/client";
import { useOperation } from "../api/queries";
import { Empty, ErrorNotice, Loading, Time } from "../components/misc";
import { StatusBadge } from "../components/StatusBadge";
import { label, parseTs } from "../format";

export function OperationDetailPage() {
  const { operationId = "" } = useParams();
  const op = useOperation(operationId);
  const logEnd = useRef<HTMLLIElement>(null);
  const logLength = op.data?.log.length ?? 0;

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: "nearest" });
  }, [logLength]);

  if (op.isPending) return <Loading />;
  if (op.error instanceof ApiError && op.error.status === 404) {
    return <Empty>Operation <code>{operationId}</code> not found.</Empty>;
  }
  if (!op.data) return <ErrorNotice error={op.error} />;
  const o = op.data;
  const live = isActive(o.status);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>
          {label(o.type)} <StatusBadge status={o.status} />
        </h1>
        <Link to="/operations">← All Operations</Link>
      </div>
      {o.error && (
        <div className="notice notice-danger" role="alert">
          <strong>Failed:</strong> {o.error}
        </div>
      )}
      <section className="card">
        <dl className="kv">
          <dt>ID</dt>
          <dd>
            <code>{o.id}</code>
          </dd>
          <dt>Cluster</dt>
          <dd>
            {o.cluster_id ? (
              <Link to={`/clusters/${o.cluster_id}`}>
                <code>{o.cluster_id}</code>
              </Link>
            ) : (
              "—"
            )}
          </dd>
          <dt>Actor</dt>
          <dd>{o.actor}</dd>
          <dt>Created</dt>
          <dd>
            <Time value={o.created_at} />
          </dd>
          <dt>Started</dt>
          <dd>
            <Time value={o.started_at} />
          </dd>
          <dt>Finished</dt>
          <dd>
            <Time value={o.finished_at} />
          </dd>
          {Object.keys(o.params).length > 0 && (
            <>
              <dt>Parameters</dt>
              <dd>
                <pre>{JSON.stringify(o.params, null, 2)}</pre>
              </dd>
            </>
          )}
        </dl>
      </section>
      <section className="card">
        <div className="page-head" style={{ marginBottom: "0.5rem" }}>
          <h2 style={{ margin: 0 }}>Step log</h2>
          <span className="muted small-text" role="status">
            {live ? "Live: refreshing every 2 s" : "Final"}
          </span>
        </div>
        {o.log.length === 0 ? (
          <p className="muted">{live ? "Waiting for the first step…" : "No steps logged."}</p>
        ) : (
          <ol className="log" aria-live="polite">
            {o.log.map((l, i) => (
              <li key={i} ref={i === o.log.length - 1 ? logEnd : undefined}>
                <time dateTime={l.ts}>{parseTs(l.ts).toLocaleTimeString()}</time>
                <span>{l.message}</span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
