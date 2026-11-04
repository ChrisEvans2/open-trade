"""Freeze review evidence and preserve per-attempt review submission identity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_review_inputs"
down_revision = "0004_scheduler_cursor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "job_attempts",
        sa.Column("review_validation_failures", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column("reviews", sa.Column("attempt_id", sa.UUID, nullable=True))
    op.add_column("reviews", sa.Column("submission_hash", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_reviews_attempt_id_job_attempts", "reviews", "job_attempts", ["attempt_id"], ["id"]
    )
    op.create_unique_constraint("uq_reviews_attempt_id", "reviews", ["attempt_id"])
    op.create_table(
        "review_inputs",
        sa.Column("job_id", sa.UUID, nullable=False),
        sa.Column("engine_id", sa.UUID, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cases", JSONB, nullable=False),
        sa.Column("evidence_ids", JSONB, nullable=False),
        sa.Column("metrics", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="pk_review_inputs"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_review_inputs_job_id_jobs"),
        sa.ForeignKeyConstraint(
            ["engine_id"], ["engines.id"], name="fk_review_inputs_engine_id_engines"
        ),
        sa.CheckConstraint("range_start < range_end", name="ck_review_inputs_valid_range"),
    )
    op.execute(
        "CREATE TRIGGER ta_immutable_review_inputs BEFORE UPDATE OR DELETE ON review_inputs "
        "FOR EACH ROW EXECUTE FUNCTION ta_reject_fact_mutation()"
    )
    op.execute(
        "CREATE TRIGGER ta_immutable_truncate_review_inputs BEFORE TRUNCATE ON review_inputs "
        "FOR EACH STATEMENT EXECUTE FUNCTION ta_reject_fact_mutation()"
    )


def downgrade():
    op.drop_table("review_inputs")
    op.drop_constraint("uq_reviews_attempt_id", "reviews", type_="unique")
    op.drop_constraint("fk_reviews_attempt_id_job_attempts", "reviews", type_="foreignkey")
    op.drop_column("reviews", "submission_hash")
    op.drop_column("reviews", "attempt_id")
    op.drop_column("job_attempts", "review_validation_failures")
