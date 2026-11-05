# Design grilling (2026-11-05)

Skill used: `grill-with-docs` (= `grilling` + `domain-modeling`). Claude asked every frontier
decision as a numbered question with a recommended answer; the human answered per number.
Each round only contained questions whose prerequisites were already settled.

**Opening prompt (human, Russian):** "install grill-with-docs to analyse this task, tell me what
needs to be done, how you'll do it, let's make a plan. This is for ScyllaDB."

## Round 1 – framing
| # | Question | Decision |
|---|---|---|
| 1 | Cloud | AWS |
| 2 | Real cloud vs local | Real AWS, destroy after each session, plus local docker-compose for dev |
| 3 | Where the Control Plane runs | Locally (ADR-0001) |
| 4 | Language | Python + FastAPI |
| 5 | Time budget | 2–3 intensive days with parallel agents |
| 6 | One or many Clusters | Model for N, demo one; one Monitor Node per Cluster |
| 7 | Repo / CI | GitHub monorepo + Actions |
| 8 | Docs language | English |

## Round 2 – architecture
| # | Question | Decision |
|---|---|---|
| 1 | Provisioning tool | Terraform, state per Cluster, `base` stack (ADR-0002) |
| 2 | Node configuration | cloud-init; Packer AMI noted for production |
| 3 | Network access | Public subnet + operator-IP allowlist + SSM (ADR-0004) |
| 4 | Instance types | i4i.large Data Nodes, t3.xlarge Monitor Node, latest ScyllaDB |
| 5 | Lifecycle scope | create, list/get, delete, rolling restart, Node Replacement, Backup now/schedule, Restore; no scale/upgrade |
| 6 | Long-running work | Operation table + in-process asyncio worker (ADR-0003) |
| 7 | Failure ladder | systemd restart → Node Replacement after grace → Monitor Node recreation; Auto-recovery on by default |
| 8 | Audit and logs | audit table + JSON logs + node logs to Loki; CloudTrail mentioned |
| 9 | API auth | Static API key → Actor |
| 10 | UI | React + Vite + TS, uses only the public API |

## Round 3 – data protection, CI, delivery
| # | Question | Decision |
|---|---|---|
| 1 | Backup location | One bucket, `clusters/<id>/` prefix, SSE, 30-day lifecycle |
| 2 | Schedule | Daily, retention 7, plus Backup now |
| 3 | Restore target | New Cluster from Backup (ADR-0005); in-place restore is a stretch goal |
| 4 | Delete semantics | Backups survive Cluster deletion; delete needs name confirmation |
| 5 | Local driver | Yes: `aws` + `local` drivers (ADR-0006) |
| 6 | CI | ruff, mypy, pytest, integration on local driver, terraform fmt/validate/tflint/checkov, UI lint/build, docker build; manual AWS e2e via OIDC |
| 7 | In-cluster security | PasswordAuthenticator, password in Secrets Manager; no TLS (gap) |
| 8 | CQL client | Python, RF=3, QUORUM, `--verify` mode |
| 9 | Region | eu-central-1 |
| 10 | Agents | Claude orchestrates; contracts first; 5 agents in worktrees |
| 11 | AI-process evidence | This folder + plan and retrospective decks |

## Round 4 – behaviour
| # | Question | Decision |
|---|---|---|
| 1 | Cluster status model | Provisioning → Healthy ⇄ Degraded → Recovering; Failed; Deleting/Deleted. Lost Monitor Node = Degraded |
| 2 | Concurrency | One active Operation per Cluster (409); Auto-recovery pre-empts Backup |
| 3 | API resources | As implemented in `control-plane/src/dbaas/api/routes.py` |
| 4 | UI screens | Clusters, Create, Cluster detail, Backups, Operations, Audit |
| 5 | Definition of done | 10-step demo on real AWS, recorded on video |
| 6 | Cut order | create/delete+CQL > Grafana > Backup/Restore > Audit > Auto-recovery > CI > local driver > rolling restart > AWS e2e; AI docs never cut |

Observation: the human accepted every recommendation (rounds 2–4 with "yes to all").
That is efficient but means the quality of the plan rests on the recommendations — which
is why each is recorded with its rationale in ADRs.
