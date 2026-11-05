"""Resumable public trade pagination; historical facts stay unchanged."""

import sqlalchemy as sa
from alembic import op

from trading_agent.storage.trade_checkpoint_schema import register

revision = "0012_trade_checkpoints"
down_revision = "0011_trade_minutes"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    tables = register(metadata)
    metadata.create_all(op.get_bind(), tables=list(tables.values()))


def downgrade():
    raise RuntimeError("保留历史事实；只允许向前迁移")
