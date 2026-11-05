"""PostgreSQL schema. Core statements keep locking/transaction ownership explicit."""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from trading_agent.storage import workflow_schema
from trading_agent.storage.prediction_schema import (
    FACT_TABLES,
    NEW_JOB_KINDS,
    OLD_JOB_KINDS,
    register,
)
from trading_agent.storage.trade_checkpoint_schema import register as register_trade_checkpoints
from trading_agent.storage.trade_schema import register as register_trade_tables

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
AMOUNT = sa.Numeric(28, 12)
PROBABILITY = sa.Numeric(12, 9)
TIME = sa.DateTime(timezone=True)


def ident():
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4)


def stamp(name="created_at"):
    return sa.Column(name, TIME, nullable=False, server_default=sa.text("clock_timestamp()"))


def json_col(name, nullable=False):
    return sa.Column(name, JSONB, nullable=nullable)


def ref(name, target, nullable=False):
    return sa.Column(name, UUID(as_uuid=True), sa.ForeignKey(target), nullable=nullable)


def enum_check(column, choices):
    return sa.CheckConstraint(
        f"{column} IN ({','.join(repr(c) for c in choices)})", name=f"{column}_values"
    )


runtime_control = sa.Table(
    "runtime_control",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("active_instance_id", UUID(as_uuid=True)),
    sa.Column("lock_backend_pid", sa.Integer),
    sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("activated_at", TIME),
    sa.Column("forecast_cursor", TIME),
    sa.CheckConstraint("id = 1", name="singleton"),
)
engines = sa.Table(
    "engines",
    metadata,
    ident(),
    sa.Column("hermes_commit", sa.String(40), nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("prompt_hash", sa.String(64), nullable=False),
    sa.Column("feature_version", sa.Text, nullable=False),
    json_col("config_json"),
    sa.Column("engine_hash", sa.String(64), nullable=False, unique=True),
    stamp(),
)
strategy_versions = sa.Table(
    "strategy_versions",
    metadata,
    ident(),
    sa.Column("version", sa.Integer, nullable=False, unique=True),
    ref("engine_id", "engines.id"),
    ref("parent_id", "strategy_versions.id", True),
    sa.Column("entry_threshold", PROBABILITY, nullable=False),
    json_col("params_json"),
    sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("state", sa.Text, nullable=False),
    stamp(),
    sa.CheckConstraint("entry_threshold >= 0.5 AND entry_threshold <= 1", name="threshold_range"),
    enum_check(
        "state",
        (
            "DRAFT",
            "FORWARD_VALIDATING",
            "AWAITING_APPROVAL",
            "SCHEDULED",
            "ACTIVE",
            "RETIRED",
            "REJECTED",
            "INSUFFICIENT_EVIDENCE",
            "INVALIDATED",
        ),
    ),
)
jobs = sa.Table(
    "jobs",
    metadata,
    ident(),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("dedupe_key", sa.Text, unique=True),
    json_col("payload"),
    sa.Column("state", sa.Text, nullable=False, server_default="QUEUED"),
    sa.Column("stage", sa.Text, nullable=False, server_default="PLANNED"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_owner", UUID(as_uuid=True)),
    sa.Column("lease_until", TIME),
    sa.Column("fence", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("runtime_generation", sa.BigInteger),
    sa.Column("next_run_at", TIME, nullable=False),
    sa.Column("deadline_at", TIME),
    sa.Column("first_model_started_at", TIME),
    sa.Column("result_id", UUID(as_uuid=True)),
    json_col("last_error", True),
    sa.Column("data_retry_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("first_data_missing_at", TIME),
    sa.Column("review_validation_failures", sa.Integer, nullable=False, server_default="0"),
    stamp(),
    stamp("updated_at"),
    enum_check("kind", OLD_JOB_KINDS + NEW_JOB_KINDS),
    enum_check(
        "state",
        ("QUEUED", "RUNNING", "RETRY_WAIT", "AWAITING_DATA", "SUCCEEDED", "FAILED", "SKIPPED"),
    ),
    sa.CheckConstraint("fence >= 0 AND attempts >= 0", name="positive_attempt"),
)
sa.Index("ix_jobs_ready", jobs.c.state, jobs.c.next_run_at)
job_attempts = sa.Table(
    "job_attempts",
    metadata,
    ident(),
    ref("job_id", "jobs.id"),
    sa.Column("fence", sa.BigInteger, nullable=False),
    sa.Column("runtime_instance_id", UUID(as_uuid=True), nullable=False),
    sa.Column("runtime_generation", sa.BigInteger, nullable=False),
    stamp("started_at"),
    sa.Column("model_started_at", TIME),
    sa.Column("ended_at", TIME),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("agent_session_id", sa.Text),
    sa.Column("process_id", sa.Integer),
    sa.Column("process_started_at", sa.Float),
    json_col("sandbox_ref", True),
    sa.Column("token_hash", sa.String(64), unique=True),
    sa.Column("token_expires_at", TIME),
    sa.Column("token_revoked", sa.Boolean, nullable=False, server_default=sa.false()),
    ref("bundle_id", "context_bundles.id", True),
    json_col("usage_json", True),
    sa.Column("error_code", sa.Text),
    sa.Column("review_validation_failures", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("job_id", "fence"),
    enum_check("status", ("RUNNING", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED")),
)
schedule_slots = sa.Table(
    "schedule_slots",
    metadata,
    ident(),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("scheduled_at", TIME, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("reason", sa.Text),
    ref("job_id", "jobs.id", True),
    sa.UniqueConstraint("kind", "scheduled_at"),
    enum_check("status", ("PLANNED", "QUEUED", "PUBLISHED", "SKIPPED", "FAILED", "PAUSED")),
)
market_bars = sa.Table(
    "market_bars",
    metadata,
    ident(),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("inst_id", sa.Text, nullable=False),
    sa.Column("interval", sa.Text, nullable=False),
    sa.Column("open_at", TIME, nullable=False),
    sa.Column("close_boundary", TIME, nullable=False),
    *[
        sa.Column(k, AMOUNT, nullable=False)
        for k in ("o", "h", "l", "c", "volume_contracts", "volume_base", "turnover_quote")
    ],
    sa.Column("available_at", TIME, nullable=False),
    sa.Column("revision_no", sa.Integer, nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.UniqueConstraint("source", "inst_id", "interval", "open_at", "revision_no"),
    sa.CheckConstraint(
        "l > 0 AND l <= o AND l <= c AND h >= o AND h >= c AND "
        "h != 'NaN'::numeric AND volume_contracts >= 0 AND "
        "volume_base >= 0 AND turnover_quote >= 0 AND "
        "volume_contracts != 'NaN'::numeric AND volume_base != 'NaN'::numeric "
        "AND turnover_quote != 'NaN'::numeric",
        name="valid_ohlcv",
    ),
    sa.CheckConstraint(
        "revision_no > 0 AND open_at < close_boundary AND close_boundary <= available_at",
        name="valid_time_revision",
    ),
    enum_check("interval", ("1m", "1h", "4h", "6h", "12h")),
)
sa.Index(
    "ix_market_bars_lookup", market_bars.c.inst_id, market_bars.c.interval, market_bars.c.open_at
)
market_observations = sa.Table(
    "market_observations",
    metadata,
    ident(),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("inst_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("event_at", TIME, nullable=False),
    sa.Column("available_at", TIME, nullable=False),
    sa.Column("coverage_start", TIME),
    sa.Column("coverage_end", TIME),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("quality", sa.Text, nullable=False),
    sa.Column("revision_no", sa.Integer, nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    json_col("payload"),
    sa.UniqueConstraint("source", "inst_id", "kind", "event_at", "revision_no"),
    sa.CheckConstraint("revision_no > 0", name="positive_revision"),
    enum_check(
        "quality",
        (
            "OK",
            "MISSING",
            "INSUFFICIENT_HISTORY",
            "PARTIAL_COVERAGE",
            "DEGENERATE",
            "NOT_APPLICABLE",
        ),
    ),
)
snapshots = sa.Table(
    "snapshots",
    metadata,
    ident(),
    sa.Column("anchor_at", TIME, nullable=False),
    sa.Column("decision_at", TIME, nullable=False),
    json_col("bar_ids_by_timeframe"),
    json_col("model_view_bar_ids"),
    json_col("features_by_timeframe"),
    json_col("external_observation_ids"),
    sa.Column("feature_spec_version", sa.Text, nullable=False),
    json_col("last_close_by_timeframe"),
    json_col("payload"),
    sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
    stamp(),
)
job_inputs = sa.Table(
    "job_inputs",
    metadata,
    ref("job_id", "jobs.id"),
    ref("snapshot_id", "snapshots.id"),
    ref("engine_id", "engines.id"),
    ref("strategy_id", "strategy_versions.id", True),
    json_col("case_ids"),
    json_col("observation_ids"),
    json_col("stats"),
    sa.Column("context_text", sa.Text, nullable=False),
    stamp(),
    sa.PrimaryKeyConstraint("job_id"),
)
trend_analyses = sa.Table(
    "trend_analyses",
    metadata,
    ident(),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("inst_id", sa.Text, nullable=False),
    sa.Column("timeframe", sa.Text, nullable=False),
    sa.Column("last_close_boundary", TIME, nullable=False),
    ref("input_snapshot_id", "snapshots.id"),
    sa.Column("input_content_hash", sa.String(64), nullable=False),
    ref("engine_id", "engines.id"),
    ref("producer_job_id", "jobs.id"),
    ref("producer_attempt_id", "job_attempts.id"),
    sa.Column("hermes_child_id", sa.Text, nullable=False),
    sa.Column("trend_state", sa.Text, nullable=False),
    sa.Column("trend_strength", PROBABILITY, nullable=False),
    json_col("notes"),
    json_col("evidence_ids"),
    sa.Column("available_at", TIME, nullable=False),
    sa.Column("valid_before", TIME, nullable=False),
    json_col("payload"),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.UniqueConstraint(
        "source", "inst_id", "timeframe", "last_close_boundary", "input_content_hash", "engine_id"
    ),
    enum_check("timeframe", ("4h", "6h", "12h")),
    enum_check("trend_state", ("UP", "RANGE", "DOWN")),
    sa.CheckConstraint("trend_strength >= 0 AND trend_strength <= 1", name="strength_range"),
    sa.CheckConstraint("last_close_boundary < valid_before", name="valid_interval"),
)
context_bundles = sa.Table(
    "context_bundles",
    metadata,
    ident(),
    ref("job_id", "jobs.id"),
    ref("snapshot_id", "snapshots.id"),
    json_col("evidence_ids"),
    json_col("observation_ids"),
    json_col("trend_analysis_ids"),
    json_col("stats"),
    sa.Column("rendered_text", sa.Text, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
    stamp(),
    sa.UniqueConstraint("job_id"),
)
timeframe_analyses = sa.Table(
    "timeframe_analyses",
    metadata,
    ident(),
    ref("attempt_id", "job_attempts.id"),
    ref("bundle_id", "context_bundles.id"),
    sa.Column("timeframe", sa.Text, nullable=False),
    sa.Column("hermes_child_id", sa.Text, nullable=False),
    *[sa.Column(k, PROBABILITY, nullable=False) for k in ("p_up", "p_flat", "p_down")],
    sa.Column("reason", sa.Text, nullable=False),
    json_col("risk_notes"),
    json_col("evidence_ids"),
    sa.Column("schema_valid", sa.Boolean, nullable=False),
    json_col("payload"),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    stamp(),
    sa.UniqueConstraint("attempt_id", "timeframe"),
    sa.CheckConstraint("timeframe = '1h'", name="one_hour"),
    sa.CheckConstraint(
        "p_up BETWEEN 0 AND 1 AND p_flat BETWEEN 0 AND 1 AND p_down BETWEEN 0 AND 1 "
        "AND abs(p_up+p_flat+p_down-1) <= 0.0000010015",
        name="valid_probability",
    ),
)
forecasts = sa.Table(
    "forecasts",
    metadata,
    ident(),
    ref("job_id", "jobs.id"),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("anchor_at", TIME, nullable=False),
    sa.Column("start_at", TIME, nullable=False),
    sa.Column("end_at", TIME, nullable=False),
    ref("engine_id", "engines.id"),
    ref("strategy_id", "strategy_versions.id", True),
    ref("bundle_id", "context_bundles.id"),
    ref("one_hour_analysis_id", "timeframe_analyses.id"),
    json_col("trend_analysis_ids"),
    *[sa.Column(k, PROBABILITY, nullable=False) for k in ("p_up", "p_flat", "p_down")],
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    json_col("risk_notes"),
    json_col("evidence_ids"),
    json_col("payload"),
    json_col("submission_payload"),
    sa.Column("submission_hash", sa.String(64), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("published_at", TIME, nullable=False),
    sa.UniqueConstraint("symbol", "anchor_at"),
    sa.UniqueConstraint("job_id"),
    enum_check("label", ("UP", "FLAT", "DOWN")),
    sa.CheckConstraint(
        "p_up BETWEEN 0 AND 1 AND p_flat BETWEEN 0 AND 1 AND p_down BETWEEN 0 AND 1 "
        "AND abs(p_up+p_flat+p_down-1) <= 0.0000010015",
        name="valid_probability",
    ),
    sa.CheckConstraint(
        "start_at = anchor_at + interval '5 minutes' AND "
        "end_at = start_at + interval '1 hour' AND "
        "published_at <= anchor_at + interval '4 minutes'",
        name="valid_window",
    ),
)
sa.Index("ix_forecasts_end", forecasts.c.end_at)
pricing_records = sa.Table(
    "pricing_records",
    metadata,
    ident(),
    ref("forecast_id", "forecasts.id"),
    sa.Column("endpoint", sa.Text, nullable=False),
    ref("bar_id", "market_bars.id"),
    stamp(),
    sa.UniqueConstraint("forecast_id", "endpoint", "bar_id"),
    enum_check("endpoint", ("START", "END")),
)
settlements = sa.Table(
    "settlements",
    metadata,
    ident(),
    ref("forecast_id", "forecasts.id"),
    sa.Column("revision_no", sa.Integer, nullable=False),
    ref("start_bar_id", "market_bars.id"),
    ref("end_bar_id", "market_bars.id"),
    sa.Column("realized_return", AMOUNT, nullable=False),
    sa.Column("actual_label", sa.Text, nullable=False),
    sa.Column("correct", sa.Boolean, nullable=False),
    sa.Column("brier", AMOUNT, nullable=False),
    stamp("settled_at"),
    ref("supersedes_id", "settlements.id", True),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.UniqueConstraint("forecast_id", "revision_no"),
    sa.UniqueConstraint("forecast_id", "start_bar_id", "end_bar_id", name="uq_settlement_prices"),
    enum_check("actual_label", ("UP", "FLAT", "DOWN")),
    sa.CheckConstraint(
        "revision_no > 0 AND brier BETWEEN 0 AND 2.000000000001", name="valid_score"
    ),
)
observations = sa.Table(
    "observations",
    metadata,
    ident(),
    sa.Column("kind", sa.Text, nullable=False),
    ref("engine_id", "engines.id"),
    sa.Column("regime", sa.Text),
    json_col("evidence_ids"),
    json_col("stats"),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("verification_status", sa.Text, nullable=False),
    sa.Column("available_at", TIME, nullable=False),
    sa.Column("expires_at", TIME, nullable=False),
    ref("supersedes_id", "observations.id", True),
    enum_check("kind", ("observation", "hypothesis")),
    enum_check("verification_status", ("VERIFIED", "REJECTED", "CANDIDATE")),
)
sa.Index(
    "ix_observations_lookup",
    observations.c.engine_id,
    observations.c.available_at,
    observations.c.expires_at,
)
reviews = sa.Table(
    "reviews",
    metadata,
    ident(),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("range_start", TIME, nullable=False),
    sa.Column("range_end", TIME, nullable=False),
    sa.Column("revision_no", sa.Integer, nullable=False),
    json_col("evidence_ids"),
    json_col("metrics"),
    sa.Column("narrative", sa.Text, nullable=False),
    ref("attempt_id", "job_attempts.id", True),
    sa.Column("submission_hash", sa.String(64)),
    stamp(),
    sa.UniqueConstraint("scope", "range_start", "range_end", "revision_no"),
    sa.UniqueConstraint("attempt_id"),
    sa.CheckConstraint("range_start < range_end AND revision_no > 0", name="valid_review"),
)
review_inputs = sa.Table(
    "review_inputs",
    metadata,
    ref("job_id", "jobs.id"),
    ref("engine_id", "engines.id"),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("range_start", TIME, nullable=False),
    sa.Column("range_end", TIME, nullable=False),
    sa.Column("decision_at", TIME, nullable=False),
    json_col("cases"),
    sa.Column("source_revision_counter", sa.BigInteger, nullable=False, server_default="0"),
    json_col("evidence_ids"),
    json_col("metrics"),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.PrimaryKeyConstraint("job_id"),
    sa.CheckConstraint("range_start < range_end", name="valid_range"),
)
review_refreshes = sa.Table(
    "review_refreshes",
    metadata,
    sa.Column("range_start", TIME, primary_key=True),
    sa.Column("range_end", TIME, nullable=False),
    sa.Column("revision_counter", sa.BigInteger, nullable=False, server_default="1"),
    sa.Column("scheduled_counter", sa.BigInteger, nullable=False, server_default="0"),
    ref("job_id", "jobs.id", True),
    sa.CheckConstraint(
        "range_start < range_end AND revision_counter >= scheduled_counter", name="valid_refresh"
    ),
)
experiments = sa.Table(
    "experiments",
    metadata,
    ident(),
    ref("incumbent_id", "strategy_versions.id"),
    ref("candidate_id", "strategy_versions.id", True),
    json_col("train_forecast_ids"),
    sa.Column("validation_start", TIME),
    json_col("validation_ids"),
    sa.Column("dataset_hash", sa.String(64), nullable=False),
    sa.Column("report_hash", sa.String(64)),
    sa.Column("state", sa.Text, nullable=False),
    json_col("metrics"),
    stamp(),
    enum_check(
        "state",
        (
            "TRAINING",
            "FORWARD_VALIDATING",
            "AWAITING_APPROVAL",
            "APPROVED",
            "REJECTED",
            "INSUFFICIENT_EVIDENCE",
            "INVALIDATED",
            "NO_CANDIDATE",
        ),
    ),
)
approvals = sa.Table(
    "approvals",
    metadata,
    ident(),
    ref("strategy_id", "strategy_versions.id"),
    ref("experiment_id", "experiments.id"),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("report_hash", sa.String(64), nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("decision", sa.Text, nullable=False),
    stamp(),
    enum_check("decision", ("APPROVE", "REJECT")),
)
strategy_activations = sa.Table(
    "strategy_activations",
    metadata,
    ident(),
    ref("strategy_id", "strategy_versions.id"),
    ref("previous_id", "strategy_versions.id", True),
    sa.Column("effective_anchor_at", TIME, nullable=False, unique=True),
    ref("approval_id", "approvals.id", True),
    sa.Column("reason", sa.Text),
    stamp(),
)
idempotency_requests = sa.Table(
    "idempotency_requests",
    metadata,
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("route", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("response_status", sa.Integer, nullable=False),
    json_col("response_body"),
    stamp(),
    sa.PrimaryKeyConstraint("actor_id", "route", "key"),
)
outbox = sa.Table(
    "outbox",
    metadata,
    ident(),
    sa.Column("event_key", sa.Text, nullable=False, unique=True),
    sa.Column("target", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="PENDING"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("next_attempt_at", TIME, nullable=False),
    sa.Column("claim_id", UUID(as_uuid=True)),
    sa.Column("lease_until", TIME),
    sa.Column("transport_message_id", sa.Text),
    sa.Column("last_error", sa.Text),
    stamp(),
    enum_check("status", ("PENDING", "SENDING", "RETRY_WAIT", "UNKNOWN", "SENT", "FAILED")),
    sa.CheckConstraint("attempts >= 0", name="valid_attempts"),
)
sa.Index("ix_outbox_ready", outbox.c.status, outbox.c.next_attempt_at)
audit_events = sa.Table(
    "audit_events",
    metadata,
    ident(),
    sa.Column("actor_type", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("object_type", sa.Text, nullable=False),
    sa.Column("object_id", UUID(as_uuid=True)),
    sa.Column("before_hash", sa.String(64)),
    sa.Column("after_hash", sa.String(64)),
    sa.Column("request_id", sa.Text, nullable=False),
    stamp(),
)
sandbox_resources = sa.Table(
    "sandbox_resources",
    metadata,
    ident(),
    ref("attempt_id", "job_attempts.id"),
    sa.Column("sandbox_id", sa.Text, unique=True),
    json_col("metadata"),
    sa.Column("policy_hash", sa.String(64), nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("next_cleanup_at", TIME),
    sa.Column("cleanup_attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    stamp(),
    enum_check("state", ("CREATING", "ACTIVE", "CLEANUP_PENDING", "TERMINATED", "UNRESOLVED")),
)

snapshot_bars = sa.Table(
    "snapshot_bars",
    metadata,
    ref("snapshot_id", "snapshots.id"),
    sa.Column("timeframe", sa.Text, nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    ref("bar_id", "market_bars.id"),
    sa.PrimaryKeyConstraint("snapshot_id", "timeframe", "position"),
    sa.CheckConstraint("position BETWEEN 0 AND 79", name="position_range"),
    enum_check("timeframe", ("1h", "4h", "6h", "12h")),
)
bundle_trends = sa.Table(
    "bundle_trends",
    metadata,
    ref("bundle_id", "context_bundles.id"),
    sa.Column("timeframe", sa.Text, nullable=False),
    ref("trend_id", "trend_analyses.id"),
    sa.PrimaryKeyConstraint("bundle_id", "timeframe"),
    enum_check("timeframe", ("4h", "6h", "12h")),
)
forecast_trends = sa.Table(
    "forecast_trends",
    metadata,
    ref("forecast_id", "forecasts.id"),
    sa.Column("timeframe", sa.Text, nullable=False),
    ref("trend_id", "trend_analyses.id"),
    sa.PrimaryKeyConstraint("forecast_id", "timeframe"),
    enum_check("timeframe", ("4h", "6h", "12h")),
)

IMMUTABLE_TABLES = (
    "engines",
    "market_bars",
    "market_observations",
    "snapshots",
    "job_inputs",
    "context_bundles",
    "trend_analyses",
    "timeframe_analyses",
    "forecasts",
    "pricing_records",
    "settlements",
    "observations",
    "reviews",
    "review_inputs",
    "approvals",
    "strategy_activations",
    "audit_events",
    "snapshot_bars",
    "bundle_trends",
    "forecast_trends",
) + FACT_TABLES

globals().update(register(metadata))

globals().update(workflow_schema.register(metadata))
IMMUTABLE_TABLES += workflow_schema.FACT_TABLES

# Only the raw public cache expires; minute observations retain immutable triggers.
globals().update(register_trade_tables(metadata))
globals().update(register_trade_checkpoints(metadata))
