# The human's prompts

Every instruction the human gave, verbatim (Russian original, English translation), and what
it set in motion. The human made decisions and unblocked access to accounts; Claude did the
planning, coding, debugging and documentation, and dispatched the sub-agents. The pattern is
deliberate: **short prompts, long autonomous runs, with checkpoints where only a human can act**
(accounts, money, publishing).

| # | Prompt (original → translation) | What it triggered |
|---|---|---|
| 1 | *(pasted the brief)* «есть такая задача, установи grill-with-docs чтобы проанализировать эту задачу, расскажи что там нужно сделать, напиши как ты это будешь делать и тд. Составим план как это сделать. Это для компании scylladb» → "Here is the task; use grill-with-docs to analyse it, explain what needs doing and how you would do it. Let's make a plan. It's for ScyllaDB." | The grilling skill: a design interview in rounds, each question with a recommended answer ([`grilling.md`](grilling.md)) |
| 2 | «делаем на AWS / делаем всё / python and fast api / язык документации на англ» → "AWS / do everything / Python and FastAPI / docs in English" | Fixed cloud, scope, stack, doc language |
| 3–5 | «на все вопросы я выбираю тобою принятые ответы», «везде да!», «везде да» → "I take your recommended answers to all questions", "yes everywhere" | Rounds 2–4 accepted as recommended → `GLOSSARY.md` (16 terms), ADR 0001–0006, `CLAUDE.md` with a cut-order for when time runs short |
| 6 | «ГО» → "Go" | Plan deck started |
| 7 | «может код сначала?» → "Maybe code first?" (mid-turn) | Deck paused at 7/13 slides; Claude wrote the contracts and the Control Plane core, then launched 4 sub-agents in parallel git worktrees with written instructions ([`agents/`](agents/)) |
| 8 | «ты всё сделал? Что осталось, что уже работает? Закончи всё» → "Are you done? What's left, what already works? Finish everything" | Backup-sync gap closed, README with an explicit verified / not-verified table, deck finished (15 slides). Claude reported plainly that nothing had run against real Docker or AWS yet |
| 9 | «docker desktop запущен» → "Docker Desktop is running" | First real integration run: 3 bugs the mocks had hidden (AIO limit, Manager task never starting, test missing `confirm`) |
| 10 | AWS account setup, guided step by step with screenshots (plan choice, IAM user, access key, `aws configure`) | Claude found the 5-vCPU quota on the new account and filed the increase (approved in minutes); applied the base stack after an explicit "да" (yes) |
| 11 | «да, давай протести всё, добро даю» → "Yes, test everything, you have my go-ahead" | Five Cluster creations on AWS, each surfacing a new bug (package pin, file permissions, EIP race, seed deadlock, Agent start race, Manager timeout, security group hairpin), each fixed and logged; then the full demo: CQL, Grafana, kill a node → Auto-recovery, Backup, delete, Restore, audit, cleanup |
| 12 | «где этот код найти на почте?» / `gh auth login` output → "Where do I find this code in my email?" | Explained the device-code flow; asked one question (repo visibility) because it was the human's call; created a **private** repo, pushed, fixed the first CI failure |
| 13 | «пока АВС выключи, завтра включим» → "Turn AWS off for now, we'll turn it on tomorrow" | Verified zero billable resources; kept only the free base stack |
| 14 | «что дальше? Давай доделаем и запушим и отдадим им на ревью по их ТЗ как они просят» → "What's next? Let's finish, push and hand it over for review per their brief" | Gap analysis against the brief: UI and rolling restart verified on AWS, a standalone HTML presentation in the repo, a "For reviewers" map in the README, this file |

## How the prompts were used

- **Decisions stayed with the human; facts were Claude's job.** Every grilling question came with
  a recommendation, so "yes everywhere" was an informed choice, not a delegation of judgement.
  Claude never asked the human for facts it could look up (versions, quotas, error causes).
- **Spending and publishing were gated.** Claude asked before the first `terraform apply`, before
  creating billable Clusters, and before choosing repo visibility; it did not ask for routine
  steps once the go-ahead was given.
- **Honesty over optimism.** When asked "are you done?", the answer separated "written" from "ran".
  That table is what drove the Docker and AWS runs that found 12 of the 19 logged defects.
