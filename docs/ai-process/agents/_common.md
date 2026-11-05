# Common rules for every sub-agent

You are one of several agents working in parallel, each in its own git worktree of the
Scylla DBaaS repo. The orchestrator (main Claude session) merges your branch afterwards.

Before writing anything, read: `CLAUDE.md`, `GLOSSARY.md`, every file in `docs/adr/`,
`control-plane/src/dbaas/providers/base.py` (the Provider contract) and
`control-plane/src/dbaas/providers/fake.py` (reference behaviour).

Rules:
- Use GLOSSARY terms exactly (Cluster, Data Node, Monitor Node, Operation, Backup, Restore…).
- Do NOT change the Provider contract, models, schemas or API routes. If you believe the
  contract is wrong, implement around it and explain the problem in your final report.
- Stay inside the directories assigned to you. Shared files you may touch are listed.
- Verify facts about ScyllaDB / Scylla Manager / AWS against official docs (WebSearch /
  WebFetch) instead of relying on memory; cite the URLs you used in your report.
- Never run anything that creates real AWS resources or spends money. No `terraform apply`.
- Code in English, typed, linted. Python: ruff + mypy --strict must pass
  (`cd control-plane && .venv/bin/ruff check src tests && .venv/bin/mypy src`; create the venv
  with `uv venv --python 3.12 && uv pip install -e '.[dev]'` if missing).
- Commit your work on your worktree branch with clear messages ending with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Final report (under 400 words): what you built, how you verified it, what is NOT verified,
  every deviation from the plan, open questions for the human.
