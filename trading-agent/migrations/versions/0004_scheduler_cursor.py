"""Persist deployment coverage origin and missing-pricing retry progress."""

import sqlalchemy as sa
from alembic import op

revision = "0004_scheduler_cursor"
down_revision = "0003_forecast_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runtime_control", sa.Column("forecast_cursor", sa.DateTime(timezone=True)))
    op.add_column(
        "jobs", sa.Column("data_retry_count", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column("jobs", sa.Column("first_data_missing_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("jobs", "first_data_missing_at")
    op.drop_column("jobs", "data_retry_count")
    op.drop_column("runtime_control", "forecast_cursor")
