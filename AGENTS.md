# Open Trade contributor instructions

This repository contains a reusable framework. Keep personal trading records,
operator configuration, credentials and deployment evidence outside Git.

The only business runtime is `trading-agent/`. Shared deterministic execution and
risk primitives live in `packages/trading-core/`; quantitative calculations live
in `packages/trading-quant/`. Models produce analyses and intents, never direct
orders or changes to account risk limits. Do not start a second trading entry.

Treat market revisions, predictions, scores, trades and audits as append-only
facts. A task write checks authorization, lease, fence and deadline in the same
transaction. Keep external I/O outside database transactions. Missing data must
remain explicit. Distinguish fixtures, real components and deployment acceptance.

Do not activate live execution, call real order APIs, change credentials or
increase account risk while developing or testing. Public examples stay paused
with execution disabled. Never auto-discover another installation's settings.

Run relevant unit tests and isolated real PostgreSQL integration tests. Hermes
contracts use a separately supplied pinned checkout; external live tests require
explicit opt-in. Do not point tests at an operator's database or Profile.

`Feature.md` is an ignored local work log. Before a commit, summarize only the
staged work and its actual validation in `changelog.md`; include limitations and
rollback. Clear committed Feature entries only after a successful commit. Never
record secrets in logs, reports, changelog or commit messages.
