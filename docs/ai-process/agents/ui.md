# Agent: ui

Goal: the web UI. Own only `ui/`.

React 18 + Vite + TypeScript (strict) + React Router + TanStack Query. Generate the API
client/types from the Control Plane OpenAPI (`/openapi.json`; start the API with
`cd control-plane && DBAAS_PROVIDER=fake .venv/bin/uvicorn dbaas.main:app` — the fake
provider simulates the whole lifecycle with 1 s steps, use it for development).
API key entered once and kept in localStorage; base URL from `VITE_API_URL`.

Screens (Round 4, Q4): 1) Clusters list with status badges (Healthy/Degraded/Recovering/…,
text + colour, never colour alone); 2) Create Cluster dialog incl. "from Backup";
3) Cluster detail: nodes table, actions (Backup now, Rolling restart, Replace node,
Auto-recovery toggle, Delete with name confirmation), Grafana/Manager links, connection info
(password shown once with a copy button) and the cql-client command, Backup Schedule editor;
4) Backups (all, incl. those of deleted Clusters) with "Restore into new Cluster";
5) Operations with live step log (poll every 2 s while active); 6) Audit log with filters.
409 responses show which Operation is blocking. Clean, sober design; no UI kit heavier than
plain CSS modules or a small headless lib.

Verify: `npm run lint`, `npm run typecheck`, `npm run build` pass; click through every
screen against the fake provider and describe what you saw. Add `ui/Dockerfile`
(nginx serving the build).
