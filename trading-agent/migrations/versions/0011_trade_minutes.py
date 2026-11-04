"""Expiring public raw trades and bounded minute collector scheduling."""

import sqlalchemy as sa
from alembic import op

from trading_agent.storage.trade_schema import register

revision = "0011_trade_minutes"
down_revision = "0010_prediction_workflows"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    tables = register(metadata)
    metadata.create_all(op.get_bind(), tables=list(tables.values()))
    op.execute(
        sa.text(
            "CREATE INDEX ix_market_trade_minutes_asof ON market_observations "
            "(inst_id,event_at,available_at,revision_no) "
            "WHERE source='okx_trade_minutes:v1'"
        )
    )


def downgrade():
    raise RuntimeError("保留分钟聚合及预测引用；只允许向前迁移")
