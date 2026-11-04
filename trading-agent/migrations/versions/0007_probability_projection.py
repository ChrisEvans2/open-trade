"""Keep rounded probability projections from rejecting valid immutable raw reports."""

from alembic import op

revision = "0007_prob_projection"
down_revision = "0006_review_refreshes"
branch_labels = None
depends_on = None


def probability_bound(tolerance):
    return (
        "p_up BETWEEN 0 AND 1 AND p_flat BETWEEN 0 AND 1 AND p_down BETWEEN 0 AND 1 "
        f"AND abs(p_up+p_flat+p_down-1) <= {tolerance}"
    )


def upgrade():
    for table in ("timeframe_analyses", "forecasts"):
        name = f"ck_{table}_valid_probability"
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, probability_bound("0.0000010015"))
    op.drop_constraint(op.f("ck_settlements_valid_score"), "settlements", type_="check")
    op.create_check_constraint(
        op.f("ck_settlements_valid_score"),
        "settlements",
        "revision_no > 0 AND brier BETWEEN 0 AND 2.000000000001",
    )


def downgrade():
    # Narrowing bounds can reject facts that were validly accepted; never rewrite those facts.
    raise RuntimeError("Probability-bound migration requires a reviewed forward migration")
