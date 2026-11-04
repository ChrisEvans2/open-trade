"""Bounded parent workflows, event coalescing and two durable resource slots."""

import sqlalchemy as sa
from alembic import op

from trading_agent.storage.workflow_schema import FACT_TABLES, register

revision = "0010_prediction_workflows"
down_revision = "0009_prediction_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    for name in ("jobs", "job_attempts", "engines", "prediction_facts", "prediction_inputs"):
        sa.Table(name, metadata, sa.Column("id", sa.UUID, primary_key=True))
    tables = register(metadata)
    metadata.create_all(op.get_bind(), tables=list(tables.values()))
    for name in FACT_TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} "
                "FOR EACH ROW EXECUTE FUNCTION ta_reject_fact_mutation()"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON {name} "
                "FOR EACH STATEMENT EXECUTE FUNCTION ta_reject_fact_mutation()"
            )
        )
    op.execute(
        sa.text(
            "INSERT INTO model_slots(slot,state,updated_at) VALUES "
            "(0,'FREE',clock_timestamp()),(1,'FREE',clock_timestamp())"
        )
    )


def downgrade():
    raise RuntimeError("保留轮次、事件及历史预测；只允许向前迁移")
