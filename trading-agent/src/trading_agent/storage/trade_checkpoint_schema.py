"""Disposable in-flight pages, separate from expiring forensic raw cache."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


def register(metadata):
    checkpoint = sa.Table(
        "trade_minute_checkpoints",
        metadata,
        sa.Column("inst_id", sa.Text, primary_key=True),
        sa.Column("open_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("state", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    pages = sa.Table(
        "trade_minute_pages",
        metadata,
        sa.Column("inst_id", sa.Text, primary_key=True),
        sa.Column("open_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("page_no", sa.Integer, primary_key=True),
        sa.Column("body_zlib", sa.LargeBinary, nullable=False),
        sa.ForeignKeyConstraint(
            ["inst_id", "open_at"],
            ["trade_minute_checkpoints.inst_id", "trade_minute_checkpoints.open_at"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("page_no > 0", name="positive_page"),
    )
    return {t.name: t for t in (checkpoint, pages)}
