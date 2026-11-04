"""Instance gate records the dedicated PostgreSQL lock connection."""

import sqlalchemy as sa
from alembic import op

revision = "0002_runtime_gate"
down_revision = "0001_research_core"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runtime_control", sa.Column("lock_backend_pid", sa.Integer))


def downgrade():
    op.drop_column("runtime_control", "lock_backend_pid")
