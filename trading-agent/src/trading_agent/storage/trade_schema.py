"""Disposable public trade cache and rebuildable collector scheduling state.

Authoritative minute aggregates remain append-only market_observations.
No prediction references the expiring cache.
"""

import sqlalchemy as sa


def register(metadata):
    work = sa.Table(
        "trade_minute_work",
        metadata,
        sa.Column("inst_id", sa.Text, primary_key=True),
        sa.Column("open_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text),
        sa.CheckConstraint(
            "state IN ('PENDING','VERIFIED','ERROR','EXPIRED')", name="state_values"
        ),
        sa.CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
    )
    sa.Index("ix_trade_minute_due", work.c.inst_id, work.c.state, work.c.next_retry_at)
    cache = sa.Table(
        "trade_raw_cache",
        metadata,
        sa.Column("inst_id", sa.Text, primary_key=True),
        sa.Column("open_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_zlib", sa.LargeBinary, nullable=False),
        sa.Column("trade_count", sa.Integer, nullable=False),
        sa.CheckConstraint("expires_at > received_at AND trade_count >= 0", name="cache_bounds"),
    )
    sa.Index("ix_trade_raw_expiry", cache.c.expires_at)
    return {work.name: work, cache.name: cache}
