# Runtime contributor instructions

Follow the repository root AGENTS.md. This directory owns the single structural
prediction runtime and PostgreSQL migrations. Reuse shared core and quant packages.
Controller and prediction Hermes roles are separate Profiles connected through one
runtime and the existing Gateway/outbox path. Keep all model tools task-scoped.

Retain historical schemas and read-only compatibility. Never delete facts to make
a migration reversible. Keep credentials, personal datasets and installation
receipts out of this repository. Integration tests use disposable databases;
model, exchange and notification transports stay fixtures unless explicitly opted in.
