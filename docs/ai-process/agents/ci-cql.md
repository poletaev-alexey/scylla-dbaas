# Agent: ci-cql

Goal: CI workflows and the bonus CQL client. Own `.github/`, `cql-client/`, `Makefile`
at the repo root, `README.md` at the repo root (skeleton only: quickstart, architecture
link, demo script — the orchestrator will finish it).

1. `cql-client/`: Python 3.12 package `cql-client` with a CLI (`cql-client`) using
   `scylla-driver`: `--hosts`, `--username`, `--password` (or `--from-api URL --cluster ID
   --api-key KEY` to fetch `/api/v1/clusters/{id}/connection`), commands `write N`
   (keyspace `demo`, NetworkTopologyStrategy RF=3 with the right DC name, table
   `kv(id uuid, ts timestamp, payload text)`, CL=QUORUM, prepared statements, token-aware
   policy), `read`, `verify` (re-reads rows written earlier, using a manifest file, exits
   non-zero on mismatch — used after Restore). Unit tests with the session mocked; ruff + mypy.
2. `.github/workflows/ci.yml` on PR/push: jobs control-plane (uv, ruff, mypy, pytest),
   cql-client (same), terraform (fmt -check, init -backend=false + validate for base and
   cluster, tflint, checkov soft-fail with summary), ui (npm ci, lint, typecheck, build),
   docker (build control-plane and ui images, no push), integration (local driver, only
   when `DBAAS_INTEGRATION=1` var is set). Pin action versions. Use path filters sparingly.
3. `.github/workflows/aws-e2e.yml`: `workflow_dispatch` only, GitHub OIDC
   (`aws-actions/configure-aws-credentials` with `role-to-assume` from a repo variable),
   creates a Cluster via the API, runs cql-client write/verify, always destroys. Document the
   one-time IAM OIDC setup in a comment.
4. `Makefile`: `make dev` (compose up), `make test`, `make lint`, `make demo-local`.
Validate workflows with `actionlint` if you can install it (brew), otherwise say so.
