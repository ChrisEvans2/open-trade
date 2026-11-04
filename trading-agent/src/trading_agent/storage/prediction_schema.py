"""Version 0009 schema, shared by migration and runtime. Freeze after migration release.

JSON facts have strict application contracts; indexed scope, identity, revision and
foreign keys remain relational. Heads/state tables are disposable projections.
"""

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

NEW_JOB_KINDS = (
    "PREDICT_TIMEFRAME",
    "PREDICT_PARENT",
    "PREDICT_VERIFY",
    "PREDICT_REVIEW",
    "KNOWLEDGE_INDEX",
    "PREDICT_EXECUTE",
    "HORIZON_LEARN",
)
OLD_JOB_KINDS = ("FORECAST", "SETTLE", "PRICE_REFRESH", "REVIEW_DAILY", "EXPERIMENT", "EXPLAIN")
FACT_TABLES = (
    "prediction_inputs",
    "prediction_facts",
    "prediction_evaluations",
    "prediction_decisions",
    "trade_intents",
    "execution_events",
    "knowledge_versions",
    "knowledge_case_links",
    "knowledge_recalls",
    "horizon_rules",
    "horizon_trials",
    "horizon_comparisons",
)


def register(metadata):
    def ident():
        return sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4)

    def ref(name, target, nullable=False, primary_key=False):
        return sa.Column(
            name,
            UUID(as_uuid=True),
            sa.ForeignKey(target),
            nullable=nullable,
            primary_key=primary_key,
        )

    def text(name, nullable=False, **kw):
        return sa.Column(name, sa.Text, nullable=nullable, **kw)

    def stamp(name="created_at"):
        return sa.Column(
            name,
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        )

    def body(name="body"):
        return sa.Column(name, JSONB, nullable=False)

    def integer(name="revision", default="1"):
        return sa.Column(name, sa.BigInteger, nullable=False, server_default=default)

    def scope():
        return [ref("engine_id", "engines.id"), text("symbol"), text("timeframe"), text("role")]

    def table(name, *cols):
        return sa.Table(name, metadata, *cols)

    table(
        "prediction_inputs",
        ident(),
        ref("job_id", "jobs.id"),
        *scope(),
        stamp("decision_at"),
        body(),
        text("content_hash"),
        sa.UniqueConstraint("job_id"),
    )
    facts = table(
        "prediction_facts",
        ident(),
        ref("job_id", "jobs.id"),
        ref("attempt_id", "job_attempts.id"),
        ref("input_id", "prediction_inputs.id"),
        *scope(),
        stamp("published_at"),
        stamp("expires_at"),
        body(),
        text("content_hash"),
        sa.UniqueConstraint("job_id"),
        sa.CheckConstraint("published_at < expires_at", name="window"),
    )
    sa.Index(
        "ix_prediction_facts_scope",
        facts.c.engine_id,
        facts.c.symbol,
        facts.c.role,
        facts.c.timeframe,
        facts.c.published_at,
    )
    table(
        "prediction_heads",
        *scope(),
        ref("prediction_id", "prediction_facts.id", True),
        integer("generation", "0"),
        stamp("updated_at"),
        sa.PrimaryKeyConstraint("engine_id", "symbol", "timeframe", "role"),
    )
    table(
        "prediction_states",
        ref("prediction_id", "prediction_facts.id", primary_key=True),
        integer(),
        text("data_quality"),
        body(),
        stamp("checked_at"),
    )
    table(
        "prediction_evaluations",
        ident(),
        ref("prediction_id", "prediction_facts.id"),
        ref("attempt_id", "job_attempts.id"),
        integer(),
        body(),
        stamp("evaluated_at"),
        sa.UniqueConstraint("prediction_id", "revision"),
    )
    table(
        "prediction_decisions",
        ident(),
        ref("prediction_id", "prediction_facts.id"),
        ref("attempt_id", "job_attempts.id"),
        body(),
        stamp(),
        sa.UniqueConstraint("prediction_id"),
    )
    table(
        "trade_intents",
        ident(),
        ref("engine_id", "engines.id"),
        text("symbol"),
        ref("prediction_id", "prediction_facts.id"),
        ref("decision_id", "prediction_decisions.id", True),
        text("cause_key", unique=True),
        text("action"),
        sa.Column("position_id", UUID(as_uuid=True)),
        sa.Column("position_version", sa.BigInteger),
        body(),
        stamp(),
    )
    table(
        "trade_intent_states",
        ref("intent_id", "trade_intents.id", primary_key=True),
        integer(),
        text("status"),
        body(),
        stamp("updated_at"),
    )
    table(
        "execution_events",
        ident(),
        ref("intent_id", "trade_intents.id"),
        ref("attempt_id", "job_attempts.id"),
        text("event_key", unique=True),
        body(),
        stamp(),
    )
    table(
        "trading_positions",
        ref("engine_id", "engines.id", primary_key=True),
        text("symbol", primary_key=True),
        sa.Column("position_id", UUID(as_uuid=True)),
        integer(),
        text("status"),
        body(),
        stamp("updated_at"),
    )
    table(
        "trading_accounts",
        text("account_key", primary_key=True),
        integer(),
        body(),
        stamp("updated_at"),
    )
    table(
        "prediction_streams",
        ref("engine_id", "engines.id", primary_key=True),
        text("symbol", primary_key=True),
        stamp("cursor"),
        body(),
        stamp("updated_at"),
    )
    knowledge = table(
        "knowledge_versions",
        ident(),
        sa.Column("concept_id", UUID(as_uuid=True), nullable=False),
        integer(),
        ref("attempt_id", "job_attempts.id", True),
        text("symbol", True),
        text("timeframe", True),
        text("structure", True),
        text("stage", True),
        text("title"),
        text("summary"),
        text("status"),
        body(),
        text("content_hash"),
        stamp("available_at"),
        sa.UniqueConstraint("concept_id", "revision"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
    )
    sa.Index(
        "ix_knowledge_versions_scope",
        knowledge.c.symbol,
        knowledge.c.timeframe,
        knowledge.c.structure,
        knowledge.c.stage,
        knowledge.c.available_at,
    )
    table(
        "knowledge_case_links",
        ident(),
        ref("version_id", "knowledge_versions.id"),
        ref("prediction_id", "prediction_facts.id"),
        ref("evaluation_id", "prediction_evaluations.id"),
        text("relation"),
        sa.UniqueConstraint("version_id", "prediction_id"),
    )
    table(
        "knowledge_recalls",
        ident(),
        ref("input_id", "prediction_inputs.id"),
        text("query_hash"),
        body(),
        stamp(),
        sa.UniqueConstraint("input_id", "query_hash"),
    )
    table(
        "horizon_rules",
        ident(),
        ref("engine_id", "engines.id"),
        text("symbol"),
        text("timeframe"),
        text("structure"),
        text("stage"),
        sa.Column("bars", sa.Integer, nullable=False),
        ref("previous_id", "horizon_rules.id", True),
        text("reason"),
        body(),
        stamp("available_at"),
        sa.CheckConstraint("bars IN (2,3,4,6,8,12)", name="allowed_bars"),
    )
    table(
        "horizon_heads",
        ref("engine_id", "engines.id", primary_key=True),
        text("symbol", primary_key=True),
        text("timeframe", primary_key=True),
        text("structure", primary_key=True),
        text("stage", primary_key=True),
        ref("rule_id", "horizon_rules.id"),
    )
    table(
        "horizon_trials",
        ident(),
        ref("rule_id", "horizon_rules.id"),
        sa.Column("candidate_bars", sa.Integer, nullable=False),
        body(),
        stamp(),
        sa.CheckConstraint("candidate_bars IN (2,3,4,6,8,12)", name="allowed_bars"),
    )
    table(
        "horizon_trial_states",
        ref("trial_id", "horizon_trials.id", primary_key=True),
        integer(),
        text("status"),
        body(),
        stamp("updated_at"),
    )
    table(
        "horizon_comparisons",
        ident(),
        ref("trial_id", "horizon_trials.id"),
        ref("prediction_id", "prediction_facts.id"),
        text("phase"),
        body(),
        stamp(),
        sa.UniqueConstraint("trial_id", "prediction_id", "phase"),
    )
    return {
        name: metadata.tables[name]
        for name in (
            *FACT_TABLES,
            "prediction_heads",
            "prediction_states",
            "trade_intent_states",
            "trading_positions",
            "trading_accounts",
            "prediction_streams",
            "horizon_heads",
            "horizon_trial_states",
        )
    }
