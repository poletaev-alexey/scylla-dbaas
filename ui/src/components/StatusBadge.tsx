import type {
  AuditResult,
  BackupStatus,
  ClusterStatus,
  NodeStatus,
  OperationStatus,
} from "../api/client";
import { label } from "../format";
import css from "./StatusBadge.module.css";

type Tone = "ok" | "warn" | "danger" | "progress" | "neutral";

/** Each tone has its own glyph so status is never conveyed by colour alone. */
const GLYPH: Record<Tone, string> = {
  ok: "✓",
  warn: "!",
  danger: "✕",
  progress: "↻",
  neutral: "–",
};

const TONES: Record<string, Tone> = {
  // Cluster
  healthy: "ok",
  degraded: "warn",
  recovering: "progress",
  provisioning: "progress",
  deleting: "progress",
  deleted: "neutral",
  failed: "danger",
  // Node
  up: "ok",
  down: "danger",
  replacing: "progress",
  pending: "neutral",
  terminated: "neutral",
  // Operation
  running: "progress",
  succeeded: "ok",
  cancelled: "neutral",
  // Backup
  done: "ok",
  // Audit Event
  accepted: "progress",
  denied: "warn",
};

type Status = ClusterStatus | NodeStatus | OperationStatus | BackupStatus | AuditResult;

export function StatusBadge({ status }: { status: Status }) {
  const tone = TONES[status] ?? "neutral";
  return (
    <span className={`${css.badge} ${css[tone] ?? ""}`}>
      <span aria-hidden="true" className={css.glyph}>
        {GLYPH[tone]}
      </span>
      {label(status)}
    </span>
  );
}
