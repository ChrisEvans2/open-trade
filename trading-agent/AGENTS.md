# Trading Agent

Keep communication concise. This directory belongs to the hermes-trade
repository; preserve the separate Hermes checkout. The two source specifications live in
docs/specifications/. Do not connect to real order APIs or operate live trading services. Production runs only the
structural prediction runtime with PostgreSQL. Shared risk/accounting/execution code lives in
../packages/trading-core, quantitative code in ../packages/trading-quant; archive/ is not installed.
Retain migrations and read-only history; do not restore legacy schedulers or code iteration tools.
Keep the controller Hermes and its existing Gateway/TG connection. The controller is separate
from the prediction Hermes parent: it queries facts and requests bounded explanation/review jobs
through trading-bridge. Both Hermes profiles use this one runtime; notifications use outbox and
HermesSender. Do not retire the controller bridge when archiving obsolete research workflows.

Treat forecasts, source revisions, settlements, trades and audits as append-only facts. Every
write from an attempt must validate its lease, fence, authorization and deadline in the same
transaction. External I/O must not run inside a database transaction. Never replace missing data
with invented values. Keep fixture evidence distinct from live acceptance.

Run meaningful unit tests and real PostgreSQL integration tests. Record implementation decisions,
specification differences and acceptance evidence in docs/. Server migration and online acceptance
are a later user-authorized phase; do not label them completed from local tests.
