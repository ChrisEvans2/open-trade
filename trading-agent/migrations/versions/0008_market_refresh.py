"""Allow deterministic rechecks of previously settled forecast pricing."""

from alembic import op

revision = "0008_market_refresh"
down_revision = "0007_prob_projection"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_jobs_kind_values"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind_values"),
        "jobs",
        "kind IN ('FORECAST', 'SETTLE', 'PRICE_REFRESH', 'REVIEW_DAILY', 'EXPERIMENT', 'EXPLAIN')",
    )


def downgrade():
    raise RuntimeError("Retaining durable pricing refresh jobs requires a forward migration")
