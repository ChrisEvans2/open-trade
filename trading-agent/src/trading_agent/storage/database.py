from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from trading_agent.config import RuntimeConfig
from trading_agent.domain.errors import DomainError


class Database:
    def __init__(self, url: str, config: RuntimeConfig | None = None, *, clock=None):
        config = config or RuntimeConfig()
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("运行数据库必须使用 PostgreSQL/psycopg")
        options = (
            f"-c statement_timeout={config.db_statement_timeout_ms} "
            f"-c lock_timeout={config.db_lock_timeout_ms} -c timezone=UTC "
            "-c idle_in_transaction_session_timeout=10000"
        )
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=8,
            max_overflow=4,
            pool_pre_ping=True,
            connect_args={"options": options, "connect_timeout": 3},
            hide_parameters=True,
        )
        self._test_clock = clock  # Explicit injection for fixtures; never read from environment.

    async def now(self, connection):
        return self._test_clock() if self._test_clock is not None else await db_now(connection)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as connection:
            yield connection

    async def require_migration(self):
        async with self.engine.connect() as connection:
            try:
                version = await connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                )
            except sa.exc.DBAPIError as exc:
                raise DomainError("MIGRATION_REQUIRED", "数据库尚未完成迁移", 503) from exc
            if version != "0011_trade_minutes":
                raise DomainError("MIGRATION_REQUIRED", "数据库迁移版本与代码不一致", 503)

    async def close(self):
        await self.engine.dispose()


async def db_now(connection: AsyncConnection):
    return await connection.scalar(sa.select(sa.func.clock_timestamp()))
