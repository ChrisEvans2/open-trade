"""Coalesce late settlement revisions into durable daily review refresh requests."""

import sqlalchemy as sa
from alembic import op

revision = "0006_review_refreshes"
down_revision = "0005_review_inputs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "review_inputs",
        sa.Column("source_revision_counter", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column(
        "jobs",
        sa.Column("review_validation_failures", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "review_refreshes",
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_counter", sa.BigInteger, nullable=False, server_default="1"),
        sa.Column("scheduled_counter", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("job_id", sa.UUID, nullable=True),
        sa.PrimaryKeyConstraint("range_start", name="pk_review_refreshes"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_review_refreshes_job_id_jobs"),
        sa.CheckConstraint(
            "range_start < range_end AND revision_counter >= scheduled_counter",
            name="ck_review_refreshes_valid_refresh",
        ),
    )


def downgrade():
    op.drop_table("review_refreshes")
    op.drop_column("jobs", "review_validation_failures")
    op.drop_column("review_inputs", "source_revision_counter")
