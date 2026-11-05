import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/client";
import { label, parseTs } from "../format";

/** Explains a failed request; for 409 it links the Operation that blocks the Cluster. */
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  if (error instanceof ApiError && error.status === 409) {
    return (
      <div role="alert" className="notice notice-warn">
        <strong>Blocked (409):</strong> {error.message}
        {error.activeOperationId && (
          <>
            {" "}
            <Link to={`/operations/${error.activeOperationId}`}>
              View blocking Operation {error.activeOperationId}
            </Link>
          </>
        )}
      </div>
    );
  }
  const msg =
    error instanceof ApiError
      ? `${error.status}: ${error.message}`
      : error instanceof Error
        ? error.message
        : "unexpected error";
  return (
    <div role="alert" className="notice notice-danger">
      <strong>Error</strong> {msg}
    </div>
  );
}

export function OperationStarted({ id, type }: { id: string; type: string }) {
  return (
    <div role="status" className="notice notice-ok">
      Operation started: <Link to={`/operations/${id}`}>{label(type)}</Link>{" "}
      <code>{id}</code>
    </div>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <button
      type="button"
      className="small"
      onClick={() => {
        navigator.clipboard.writeText(text).then(
          () => {
            setState("copied");
          },
          () => {
            setState("failed");
          },
        );
        setTimeout(() => {
          setState("idle");
        }, 2000);
      }}
    >
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : label}
    </button>
  );
}

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function Time({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  const d = parseTs(value);
  const sec = Math.round((d.getTime() - Date.now()) / 1000);
  const abs = Math.abs(sec);
  const rel =
    abs < 60
      ? rtf.format(sec, "second")
      : abs < 3600
        ? rtf.format(Math.round(sec / 60), "minute")
        : abs < 86400
          ? rtf.format(Math.round(sec / 3600), "hour")
          : rtf.format(Math.round(sec / 86400), "day");
  return (
    <time dateTime={value} title={d.toLocaleString()}>
      {rel}
    </time>
  );
}

export function Loading() {
  return <p className="muted">Loading…</p>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="empty">{children}</p>;
}
