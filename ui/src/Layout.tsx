import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { API_URL, getApiKey, setApiKey } from "./api/client";
import css from "./Layout.module.css";

function subscribe(cb: () => void): () => void {
  window.addEventListener("dbaas:apikey", cb);
  window.addEventListener("storage", cb);
  return () => {
    window.removeEventListener("dbaas:apikey", cb);
    window.removeEventListener("storage", cb);
  };
}

function useApiKey(): string | null {
  return useSyncExternalStore(subscribe, getApiKey);
}

const NAV = [
  ["/clusters", "Clusters"],
  ["/backups", "Backups"],
  ["/operations", "Operations"],
  ["/audit", "Audit log"],
] as const;

export function Layout() {
  const key = useApiKey();
  const qc = useQueryClient();
  const [hadKey, setHadKey] = useState(key !== null);

  useEffect(() => {
    if (key) setHadKey(true);
    else qc.clear();
  }, [key, qc]);

  return (
    <div className={css.shell}>
      <header className={css.topbar}>
        <span className={css.brand}>Scylla DBaaS</span>
        {key && (
          <nav className={css.nav} aria-label="Main">
            {NAV.map(([to, text]) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) => (isActive ? css.active : undefined)}
              >
                {text}
              </NavLink>
            ))}
          </nav>
        )}
        {key && (
          <button
            type="button"
            className={`ghost small ${css.signout}`}
            onClick={() => {
              setApiKey(null);
            }}
          >
            Change API key
          </button>
        )}
      </header>
      <main className={css.main}>
        {key ? <Outlet /> : <ApiKeyForm rejected={hadKey} />}
      </main>
    </div>
  );
}

function ApiKeyForm({ rejected }: { rejected: boolean }) {
  const [value, setValue] = useState("");
  return (
    <form
      className={`card ${css.keyForm}`}
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) setApiKey(value.trim());
      }}
    >
      <h1>Sign in</h1>
      {rejected && (
        <div role="alert" className="notice notice-warn">
          The API key was missing or rejected (401). Enter a valid key.
        </div>
      )}
      <label>
        API key
        <input
          type="password"
          autoComplete="off"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
          }}
          autoFocus
          required
        />
      </label>
      <p className="muted small-text">
        Stored in this browser&apos;s localStorage and sent as <code>X-API-Key</code> to{" "}
        <code>{API_URL || window.location.origin}</code>.
      </p>
      <button type="submit" className="primary">
        Continue
      </button>
    </form>
  );
}
