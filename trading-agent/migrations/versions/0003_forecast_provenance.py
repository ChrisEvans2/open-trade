"""Bind attempt authorization to its bundle and preserve exact submission provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_forecast_provenance"
down_revision = "0002_runtime_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("job_attempts", sa.Column("model_started_at", sa.DateTime(timezone=True)))
    op.add_column("job_attempts", sa.Column("bundle_id", sa.UUID, nullable=True))
    op.create_foreign_key(
        "fk_job_attempts_bundle_id_context_bundles",
        "job_attempts",
        "context_bundles",
        ["bundle_id"],
        ["id"],
    )
    op.add_column("forecasts", sa.Column("submission_payload", JSONB, nullable=False))
    op.add_column("forecasts", sa.Column("submission_hash", sa.String(64), nullable=False))


def downgrade():
    op.drop_column("forecasts", "submission_hash")
    op.drop_column("forecasts", "submission_payload")
    op.drop_constraint(
        "fk_job_attempts_bundle_id_context_bundles", "job_attempts", type_="foreignkey"
    )
    op.drop_column("job_attempts", "bundle_id")
    op.drop_column("job_attempts", "model_started_at")
