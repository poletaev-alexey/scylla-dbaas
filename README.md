# Scylla DBaaS

A simplified Database-as-a-Service for ScyllaDB on AWS: create and operate a Cluster
(3 Data Nodes + 1 Monitor Node running Scylla Manager and the Scylla Monitoring Stack)
through a web UI and a REST API. Assignment brief: [`task.txt`](task.txt).

> Skeleton — the final version of this README is being completed.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, Node 22 (UI), Terraform ≥ 1.6
(AWS only).

```bash
make install        # Python venvs (uv) + UI dependencies
make test           # unit tests: control-plane + cql-client
make lint           # ruff + mypy --strict, terraform fmt, UI lint/typecheck, actionlint

make api-fake       # API on :8000 with the in-memory fake Provider (no Docker, no AWS)
make dev            # Control Plane + UI via docker compose
```

API docs: <http://localhost:8000/docs> (header `X-API-Key: dev-key`).

## Architecture

- Vocabulary: [`GLOSSARY.md`](GLOSSARY.md)
- Decisions: [`docs/adr/`](docs/adr/)
- How the work was split between humans and AI agents: [`docs/ai-process/`](docs/ai-process/)

| Path | What |
|---|---|
| `control-plane/` | FastAPI API, Operation worker, Provider drivers (`fake`, `local`, `aws`) |
| `infra/` | Terraform: `base` stack, per-Cluster `cluster` module, cloud-init |
| `ui/` | React + Vite SPA on the public `/api/v1` |
| `cql-client/` | `cql-client` CLI (bonus) and the `dbaas-e2e` scenario runner |
| `.github/workflows/` | `ci.yml` (every PR), `aws-e2e.yml` (manual, GitHub OIDC) |

## CQL client

```bash
cd cql-client && uv venv && uv pip install -e .
# connection details straight from the Control Plane (password is revealed only once):
.venv/bin/cql-client --from-api http://localhost:8000 --cluster <id> --api-key dev-key write 1000
.venv/bin/cql-client --hosts <ip1>,<ip2>,<ip3> --username scylla --password '<pw>' read
.venv/bin/cql-client --hosts ... --username scylla --password '<pw>' verify   # after a Restore
```

Keyspace `demo` (NetworkTopologyStrategy, RF=3, datacenter auto-detected), table
`kv(id uuid, ts timestamp, payload text)`, prepared statements, token-aware routing,
consistency QUORUM. `write` records every row in `cql-manifest.json`; `verify` re-reads
them and exits non-zero on any missing or different row.

## Demo script

Local (Docker, `local` Provider driver):

```bash
make api-local      # terminal 1: Control Plane with DBAAS_PROVIDER=local
make demo-local     # terminal 2: create -> write -> verify -> Backup -> Restore -> verify -> delete
```

On AWS: run the **aws-e2e** workflow (Actions → aws-e2e → Run workflow); one-time IAM/OIDC
setup is documented at the top of [`.github/workflows/aws-e2e.yml`](.github/workflows/aws-e2e.yml).

Click-through demo of the UI: _TODO_.

## CI

`ci.yml` runs on every PR and push to `main`: control-plane and cql-client (ruff, mypy
--strict, pytest), terraform (fmt, validate, tflint, checkov report-only), ui (lint,
typecheck, build), docker image builds (no push), and — when the repository variable
`DBAAS_INTEGRATION=1` is set — the full lifecycle on the `local` driver.

## Assumptions, limitations, production differences

_TODO_
