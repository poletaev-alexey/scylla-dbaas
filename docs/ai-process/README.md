# How this was built: human + AI

This folder is a graded deliverable. It records how the work was split between a human
(decisions, review, demo) and Claude (orchestration, code, docs) plus sub-agents.

| File | What |
|---|---|
| `grilling.md` | The 4-round design interview that produced every decision before code |
| `agents/` | The exact instructions given to each parallel sub-agent |
| `log.md` | Chronological log of prompts, outcomes and corrections |
| `mistakes.md` | Where the AI was wrong, how it was caught, what changed |

Workflow: **grill → glossary + ADRs → plan deck → contracts → parallel agents → review → demo → retrospective deck.**
