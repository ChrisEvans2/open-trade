from __future__ import annotations

import asyncio
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database, db_now

LOCK_NAMESPACE, LOCK_ID = 2145143, 8765


async def lock_exists(connection: AsyncConnection, backend_pid: int) -> bool:
    return bool(
        await connection.scalar(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=:pid AND classid=:namespace AND objid=:key AND objsubid=2 AND granted "
                "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))"
            ),
            {"pid": backend_pid, "namespace": LOCK_NAMESPACE, "key": LOCK_ID},
        )
    )


class RuntimeOwnership:
    def __init__(self, database: Database):
        self.database = database
        self.instance_id = uuid4()
        self.generation = 0
        self.backend_pid: int | None = None
        self.connection: AsyncConnection | None = None
        self._probe_lock = asyncio.Lock()

    async def acquire(self):
        self.connection = await self.database.engine.connect()
        try:
            held = await self.connection.scalar(
                sa.select(sa.func.pg_try_advisory_lock(LOCK_NAMESPACE, LOCK_ID))
            )
            if not held:
                raise DomainError("RUNTIME_ALREADY_RUNNING", "已有runtime持有单实例锁", 409)
            self.backend_pid = await self.connection.scalar(sa.select(sa.func.pg_backend_pid()))
            await self.connection.commit()
            async with self.database.transaction() as connection:
                control = (
                    (
                        await connection.execute(
                            sa.select(s.runtime_control)
                            .where(s.runtime_control.c.id == 1)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                self.generation = control["generation"] + 1
                now = await db_now(connection)
                await connection.execute(
                    sa.update(s.runtime_control)
                    .where(s.runtime_control.c.id == 1)
                    .values(
                        active_instance_id=self.instance_id,
                        generation=self.generation,
                        lock_backend_pid=self.backend_pid,
                        activated_at=now,
                    )
                )
                # Revoke all old model authority before the new runtime can claim jobs.
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.status == "RUNNING")
                    .values(
                        status="EXPIRED",
                        token_revoked=True,
                        ended_at=now,
                    )
                )
                await connection.execute(
                    sa.update(s.jobs).where(s.jobs.c.state == "RUNNING").values(lease_until=now)
                )
        except BaseException:
            await self.release()
            raise

    async def guard(self, connection: AsyncConnection, *, exclusive=False):
        # All business writers take SHARE before the job lock. Takeover uses UPDATE.
        control = (
            (
                await connection.execute(
                    sa.select(s.runtime_control)
                    .where(s.runtime_control.c.id == 1)
                    .with_for_update(read=not exclusive)
                )
            )
            .mappings()
            .one()
        )
        if (
            control["active_instance_id"] != self.instance_id
            or control["generation"] != self.generation
            or control["lock_backend_pid"] != self.backend_pid
            or not await lock_exists(connection, self.backend_pid)
        ):
            raise StaleAttempt()

    async def probe(self) -> bool:
        async with self._probe_lock:
            if self.connection is None or self.connection.closed:
                return False
            try:
                result = await lock_exists(self.connection, self.backend_pid)
                await self.connection.commit()
                return result
            except sa.exc.DBAPIError:
                return False

    async def release(self):
        if self.connection is None:
            return
        try:
            async with self.database.transaction() as connection:
                await connection.execute(
                    sa.update(s.runtime_control)
                    .where(
                        s.runtime_control.c.id == 1,
                        s.runtime_control.c.active_instance_id == self.instance_id,
                    )
                    .values(active_instance_id=None, lock_backend_pid=None)
                )
        finally:
            try:
                if not self.connection.closed:
                    await self.connection.rollback()
                    await self.connection.execute(
                        sa.select(sa.func.pg_advisory_unlock(LOCK_NAMESPACE, LOCK_ID))
                    )
                    await self.connection.commit()
            except sa.exc.DBAPIError:
                pass
            finally:
                await self.connection.close()
                self.connection = None
