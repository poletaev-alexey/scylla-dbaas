# Log

- **2026-11-05** – Grilling (4 rounds, 35 decisions) → `GLOSSARY.md`, ADR 0001–0006.
- **2026-11-05** – Plan deck started (7/13 slides), paused on human request: code first.
- **2026-11-05** – Claude wrote the contracts and Control Plane core itself (domain, models,
  Provider protocol, fake driver, Operation runner, health checker/Auto-recovery, REST API,
  13 lifecycle tests). ruff + mypy --strict clean. Three bugs found by tests (see mistakes.md).
- **2026-11-05** – 4 agents launched in worktrees. ci-cql and ui merged; their reports surfaced 2 API bugs, fixed by the orchestrator.
