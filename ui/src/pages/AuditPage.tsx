import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuditEvents, useClusters, type AuditFilter } from "../api/queries";
import { Empty, ErrorNotice, Loading, Time } from "../components/misc";
import { StatusBadge } from "../components/StatusBadge";

function fromParams(p: URLSearchParams): AuditFilter {
  return {
    cluster_id: p.get("cluster_id") ?? "",
    actor: p.get("actor") ?? "",
    action: p.get("action") ?? "",
    limit: Number(p.get("limit") ?? 100) || 100,
  };
}

export function AuditPage() {
  const [params, setParams] = useSearchParams();
  const filter = fromParams(params);
  const [draft, setDraft] = useState(filter);
  const events = useAuditEvents(filter);
  const clusters = useClusters();
  const names = new Map((clusters.data ?? []).map((c) => [c.id, c.name]));

  const apply = (f: AuditFilter) => {
    const next: Record<string, string> = {};
    if (f.cluster_id) next.cluster_id = f.cluster_id;
    if (f.actor) next.actor = f.actor;
    if (f.action) next.action = f.action;
    if (f.limit !== 100) next.limit = String(f.limit);
    setParams(next);
  };

  return (
    <>
      <div className="page-head">
        <h1>Audit log</h1>
      </div>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          apply(draft);
        }}
      >
        <label>
          Cluster
          <select
            value={draft.cluster_id}
            onChange={(e) => {
              setDraft({ ...draft, cluster_id: e.target.value });
            }}
          >
            <option value="">Any</option>
            {(clusters.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
            {draft.cluster_id && !names.has(draft.cluster_id) && (
              <option value={draft.cluster_id}>{draft.cluster_id}</option>
            )}
          </select>
        </label>
        <label>
          Actor
          <input
            value={draft.actor}
            placeholder="e.g. dev, system"
            onChange={(e) => {
              setDraft({ ...draft, actor: e.target.value });
            }}
          />
        </label>
        <label>
          Action starts with
          <input
            value={draft.action}
            placeholder="e.g. cluster., backup"
            onChange={(e) => {
              setDraft({ ...draft, action: e.target.value });
            }}
          />
        </label>
        <label>
          Limit
          <select
            value={draft.limit}
            onChange={(e) => {
              setDraft({ ...draft, limit: Number(e.target.value) });
            }}
          >
            {[50, 100, 500, 1000].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="primary">
          Apply
        </button>
        <button
          type="button"
          onClick={() => {
            const empty = { cluster_id: "", actor: "", action: "", limit: 100 };
            setDraft(empty);
            apply(empty);
          }}
        >
          Reset
        </button>
      </form>
      <ErrorNotice error={events.error} />
      {events.isPending ? (
        <Loading />
      ) : !events.data?.length ? (
        <Empty>No Audit Events match these filters.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Target</th>
                <th>Result</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {events.data.map((e) => (
                <tr key={e.id}>
                  <td>
                    <Time value={e.ts} />
                  </td>
                  <td>{e.actor}</td>
                  <td>
                    <code>{e.action}</code>
                  </td>
                  <td className="small-text">
                    {e.target_type === "cluster" && e.target_id ? (
                      <Link to={`/clusters/${e.target_id}`}>
                        {names.get(e.target_id) ?? e.target_id}
                      </Link>
                    ) : (
                      <>
                        {e.target_type}
                        {e.target_id ? ` ${e.target_id}` : ""}
                      </>
                    )}
                  </td>
                  <td>
                    <StatusBadge status={e.result} />
                  </td>
                  <td className="small-text">
                    <AuditDetails details={e.details} />
                    {e.request_id && (
                      <div className="muted">
                        request <code>{e.request_id}</code>
                      </div>
                    )}
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

function AuditDetails({ details }: { details: Record<string, unknown> }) {
  const entries = Object.entries(details);
  if (!entries.length) return null;
  return (
    <div>
      {entries.map(([k, v]) => (
        <div key={k}>
          <span className="muted">{k}:</span>{" "}
          {k === "active_operation_id" && typeof v === "string" ? (
            <Link to={`/operations/${v}`}>
              <code>{v}</code>
            </Link>
          ) : k === "operation_id" && typeof v === "string" ? (
            <Link to={`/operations/${v}`}>
              <code>{v}</code>
            </Link>
          ) : (
            <code>{typeof v === "string" ? v : JSON.stringify(v)}</code>
          )}
        </div>
      ))}
    </div>
  );
}
