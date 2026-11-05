# Scylla DBaaS — project rules for Claude and agents

Home assignment for ScyllaDB Cloud Engineering: a simplified DBaaS for ScyllaDB on AWS. The original brief is in `task.txt`.

## Source of truth
- **Vocabulary:** `GLOSSARY.md`. Use its terms exactly in code, API, UI and docs (Cluster, Data Node, Monitor Node, Operation, Actor, Audit Event, Node Replacement, Auto-recovery, Backup, Backup Schedule, Restore). Do not use the `_Avoid_` words.
- **Decisions:** `docs/adr/`. Do not contradict an ADR; if a change requires it, propose a new ADR instead.
- **Process log:** `docs/ai-process/`. It is a graded deliverable: record prompts, agent instructions, and every place an agent was wrong and how it was caught.

## Layout
- `infra/base` – long-lived Terraform stack (VPC, backup bucket, IAM)
- `infra/cluster` – per-Cluster Terraform module (3 Data Nodes + 1 Monitor Node)
- `infra/cloud-init` – node bootstrap templates
- `control-plane/` – Python 3.12, FastAPI, SQLAlchemy, asyncio Operation worker, Provider drivers (`aws`, `local`)
- `ui/` – React + Vite + TypeScript SPA; talks only to the public `/api/v1`
- `cql-client/` – Python CLI using `scylla-driver`
- `.github/workflows/` – CI

## Conventions
- All docs, code comments and commit messages in English.
- Python: ruff + mypy (strict) + pytest. TypeScript: eslint + tsc strict.
- Every mutating API call creates an Operation and an Audit Event.
- One mutating Operation per Cluster at a time (409 otherwise); Auto-recovery pre-empts Backups.
- Never commit secrets, Terraform state, or `.env` files.
- AWS region `eu-central-1`. Always `destroy` cloud resources after a working session.

## Priorities when time is short (cut from the bottom)
1. create/delete + CQL client 2. Grafana monitoring 3. Backup/Restore 4. Audit 5. Auto-recovery 6. CI 7. `local` driver 8. rolling restart 9. AWS e2e workflow. AI-process docs are never cut.
