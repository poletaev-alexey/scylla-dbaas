# Log

- **2026-11-05** – Grilling (4 rounds, 35 decisions) → `GLOSSARY.md`, ADR 0001–0006.
- **2026-11-05** – Plan deck started (7/13 slides), paused on human request: code first.
- **2026-11-05** – Claude wrote the contracts and Control Plane core itself (domain, models,
  Provider protocol, fake driver, Operation runner, health checker/Auto-recovery, REST API,
  13 lifecycle tests). ruff + mypy --strict clean. Three bugs found by tests (see mistakes.md).
- **2026-11-05** – 4 agents launched in worktrees. ci-cql and ui merged; their reports surfaced 2 API bugs, fixed by the orchestrator.
- **2026-11-05** – scylla-ops-local merged (36 tests). Its report raised 4 contract issues; #2 fixed (recreate_monitor contract), #1 (cluster id derived from backup_location) accepted as a documented coupling, #3/#4 (host networking for local driver on macOS) recorded as limitations.
- **2026-11-05** – infra-aws merged after a mid-flight contract update. Repo-wide verification on main: control-plane 52 passed/1 skipped, cql-client 29 passed, ruff + mypy --strict clean, terraform fmt + validate OK on both stacks, UI typecheck/lint/build OK. Nothing has run on real AWS or Docker yet.
- Accepted with documentation (not fixed): Scylla REST :10000 reachable from operator CIDR without auth (ADR-0004 scope); 4 Elastic IPs per Cluster (quota 5/region); AWS CLI via snap on nodes adds boot time.
- **2026-11-06** – Scheduled-Backup sync added (gap found during planning). API smoke run on the fake Provider passed end to end. README finalised with an explicit verified / not-verified table. control-plane: 54 passed, 1 skipped.
- **2026-11-06** – Deck finished: 15 slides (plan + retrospective). Sources in docs/ai-process/deck.
- **2026-11-08** – Human started Docker Desktop. First real integration run on the `local` driver found 3 bugs (mistakes #8–#10); third run green: create → write → Backup → delete → Restore → verify in ~90 s.
- **2026-11-08** – Human created the AWS account and access key, guided step by step. Claude raised the vCPU quota (5 → 16), applied `infra/base` after an explicit yes.
- **2026-11-08** – AWS: five Cluster creations, seven AWS-only bugs found and fixed (mistakes #11–#18), each diagnosed over SSM without SSH. Then the full demo through the API: 1000 rows over CQL, Grafana (8 dashboards, 12/12 targets), terminate `data-2` → Auto-recovery by `system` → healthy in 7 min with the same IP, Backup (1480 S3 objects), delete source, Restore into a new Cluster → 1000/1000 rows, audit trail, cleanup to zero billable resources.
- **2026-11-08** – Private GitHub repo created; first CI run failed on the Docker build context (mistake #19), fixed; CI green.
- **2026-11-09** – Gap check against the brief. Sixth AWS Cluster created with no manual step; UI (all screens) and rolling restart verified against it (reads never failed); Grafana screenshot under CQL load; Cluster and Backups deleted, AWS back to zero. Also: standalone HTML presentation (`presentation.html`, built from the deck sources by `deck/build.py`); README "For reviewers" map; `prompts.md`.
