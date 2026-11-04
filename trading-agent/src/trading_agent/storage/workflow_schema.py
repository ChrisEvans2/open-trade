"""Version 0010: durable analysis coordination, separate from immutable forecasts."""

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

FACT_TABLES = ("analysis_events",)


def register(metadata):
    def ref(name, target, nullable=False):
        return sa.Column(name, UUID(as_uuid=True), sa.ForeignKey(target), nullable=nullable)

    def stamp(name, nullable=False):
        return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)

    def body(name, default="{}"):
        return sa.Column(
            name, JSONB, nullable=False, server_default=sa.text("'" + default + "'::jsonb")
        )

    def ident():
        return sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4)

    tables = {}
    tables["analysis_scopes"] = sa.Table(
        "analysis_scopes",
        metadata,
        ref("engine_id", "engines.id"),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="IDLE"),
        stamp("window_until", True),
        body("pending_roles", "[]"),
        ref("active_round_id", "analysis_rounds.id", True),
        sa.Column("parent_available", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error_group", UUID(as_uuid=True)),
        sa.Column("recoveries", sa.Integer, nullable=False, server_default="0"),
        stamp("resume_at", True),
        sa.Column("error_code", sa.Text),
        stamp("updated_at"),
        sa.PrimaryKeyConstraint("engine_id", "symbol"),
        sa.CheckConstraint("recoveries BETWEEN 0 AND 3", name="recoveries_bound"),
    )
    tables["analysis_rounds"] = sa.Table(
        "analysis_rounds",
        metadata,
        ident(),
        ref("job_id", "jobs.id"),
        ref("engine_id", "engines.id"),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="QUEUED"),
        sa.Column("recovery_index", sa.Integer, nullable=False, server_default="0"),
        stamp("started_at", True),
        stamp("deadline_at", True),
        body("counts"),
        ref("parent_result_id", "prediction_facts.id", True),
        sa.Column("error_code", sa.Text),
        stamp("created_at"),
        sa.UniqueConstraint("job_id"),
    )
    tables["analysis_runs"] = sa.Table(
        "analysis_runs",
        metadata,
        ident(),
        ref("round_id", "analysis_rounds.id"),
        ref("coordinator_attempt_id", "job_attempts.id"),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("launch_index", sa.Integer, nullable=False, server_default="0"),
        ref("job_id", "jobs.id"),
        ref("attempt_id", "job_attempts.id"),
        sa.Column("state", sa.Text, nullable=False),
        stamp("started_at"),
        stamp("execution_started_at", True),
        stamp("execution_deadline", True),
        stamp("execution_finished_at", True),
        stamp("finished_at", True),
        stamp("deadline_at"),
        ref("input_id", "prediction_inputs.id", True),
        ref("result_id", "prediction_facts.id", True),
        sa.Column("error_code", sa.Text),
        sa.UniqueConstraint("round_id", "role", "number"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("attempt_id"),
        sa.CheckConstraint("role IN ('1h','4h','6h','12h','parent')", name="role_values"),
        sa.CheckConstraint("number BETWEEN 1 AND 3", name="number_bound"),
    )
    tables["analysis_events"] = sa.Table(
        "analysis_events",
        metadata,
        ident(),
        ref("engine_id", "engines.id"),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("source_key", sa.Text, nullable=False, unique=True),
        sa.Column("kind", sa.Text, nullable=False),
        body("body"),
        stamp("created_at"),
    )
    tables["model_slots"] = sa.Table(
        "model_slots",
        metadata,
        sa.Column("slot", sa.Integer, primary_key=True),
        ref("attempt_id", "job_attempts.id", True),
        sa.Column("state", sa.Text, nullable=False, server_default="FREE"),
        stamp("updated_at"),
        sa.CheckConstraint("slot IN (0,1)", name="capacity_two"),
        sa.UniqueConstraint("attempt_id"),
    )
    return tables
