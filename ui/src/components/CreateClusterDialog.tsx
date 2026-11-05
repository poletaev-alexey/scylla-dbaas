import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { useBackups, useOperationMutation } from "../api/queries";
import { parseTs } from "../format";
import { Dialog } from "./Dialog";
import { ErrorNotice } from "./misc";

const NAME_PATTERN = "[a-z][a-z0-9\\-]{2,30}";

/** Create Cluster, optionally from a Backup (Restore, ADR-0005). */
export function CreateClusterDialog({
  onClose,
  initialBackupId = null,
}: {
  onClose: () => void;
  initialBackupId?: string | null;
}) {
  const navigate = useNavigate();
  const backups = useBackups();
  const restorable = (backups.data ?? []).filter((b) => b.status === "done");
  const [name, setName] = useState("");
  const [autoRecovery, setAutoRecovery] = useState(true);
  const [source, setSource] = useState<"empty" | "backup">(initialBackupId ? "backup" : "empty");
  const [backupId, setBackupId] = useState<string>(initialBackupId ?? "");

  const create = useOperationMutation(async () =>
    unwrap(
      await api.POST("/api/v1/clusters", {
        body: {
          name,
          auto_recovery: autoRecovery,
          from_backup_id: source === "backup" ? backupId : null,
        },
      }),
    ),
  );

  return (
    <Dialog title={source === "backup" ? "Restore into new Cluster" : "Create Cluster"} onClose={onClose}>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate(undefined, {
            onSuccess: (res) => {
              onClose();
              if (res.cluster) navigate(`/clusters/${res.cluster.id}`);
            },
          });
        }}
      >
        <label>
          Name
          <input
            value={name}
            onChange={(e) => {
              setName(e.target.value);
            }}
            pattern={NAME_PATTERN}
            required
            autoFocus
            placeholder="demo"
          />
          <span className="hint">
            3–31 characters: lowercase letters, digits and “-”, starting with a letter.
          </span>
        </label>
        <p className="muted small-text" style={{ margin: 0 }}>
          A Cluster is three Data Nodes plus one Monitor Node (Scylla Manager and Grafana).
        </p>

        <fieldset>
          <legend>Data</legend>
          <label className="inline">
            <input
              type="radio"
              name="source"
              checked={source === "empty"}
              onChange={() => {
                setSource("empty");
              }}
            />
            Empty Cluster
          </label>
          <label className="inline">
            <input
              type="radio"
              name="source"
              checked={source === "backup"}
              onChange={() => {
                setSource("backup");
              }}
            />
            From Backup (Restore)
          </label>
          {source === "backup" && (
            <label>
              Backup
              <select
                value={backupId}
                onChange={(e) => {
                  setBackupId(e.target.value);
                }}
                required
              >
                <option value="" disabled>
                  {restorable.length ? "Select a Backup…" : "No completed Backups"}
                </option>
                {restorable.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.cluster_name} · {parseTs(b.created_at).toLocaleString()} ·{" "}
                    {b.snapshot_tag ?? b.id}
                  </option>
                ))}
              </select>
            </label>
          )}
        </fieldset>

        <label className="inline">
          <input
            type="checkbox"
            checked={autoRecovery}
            onChange={(e) => {
              setAutoRecovery(e.target.checked);
            }}
          />
          Auto-recovery (replace permanently failed nodes automatically)
        </label>

        <ErrorNotice error={create.error} />

        <div className="form-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="primary"
            disabled={create.isPending || (source === "backup" && !backupId)}
          >
            {create.isPending ? "Submitting…" : source === "backup" ? "Restore" : "Create"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
