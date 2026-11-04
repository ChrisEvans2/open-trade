"""Independent structural forecasts, knowledge versions and durable trading intents."""

import sqlalchemy as sa
from alembic import op

from trading_agent.storage.prediction_schema import (
    FACT_TABLES,
    NEW_JOB_KINDS,
    OLD_JOB_KINDS,
    register,
)

revision = "0009_prediction_knowledge"
down_revision = "0008_market_refresh"
branch_labels = None
depends_on = None


def upgrade():
    # This schema module is frozen with revision 0009; subsequent changes need migrations.
    metadata = sa.MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
    for name in ("jobs", "job_attempts", "engines"):
        sa.Table(name, metadata, sa.Column("id", sa.UUID, primary_key=True))
    tables = register(metadata)
    metadata.create_all(op.get_bind(), tables=list(tables.values()))
    for name in FACT_TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE "
                f"ON {name} FOR EACH ROW EXECUTE FUNCTION ta_reject_fact_mutation()"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE "
                f"ON {name} FOR EACH STATEMENT EXECUTE FUNCTION ta_reject_fact_mutation()"
            )
        )
    op.drop_constraint(op.f("ck_jobs_kind_values"), "jobs", type_="check")
    choices = ",".join(repr(k) for k in OLD_JOB_KINDS + NEW_JOB_KINDS)
    op.create_check_constraint(op.f("ck_jobs_kind_values"), "jobs", f"kind IN ({choices})")


def downgrade():
    raise RuntimeError("预测、知识及执行事实必须保留；请使用向前迁移")
