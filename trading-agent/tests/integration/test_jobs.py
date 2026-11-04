import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa

from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.jobs.service import JobService
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.storage import schema as s
from trading_agent.storage.database import db_now

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def ownership(database):
    owner = RuntimeOwnership(database)
    await owner.acquire()
    try:
        yield owner
    finally:
        await owner.release()


async def create_job(database, jobs, *, deadline_seconds=240, key="fixture:one"):
    async with database.transaction() as connection:
        now = await db_now(connection)
        ident = await jobs.create(
            connection,
            "FORECAST",
            key,
            {"fixture": True},
            now,
            now + timedelta(seconds=deadline_seconds),
        )
    return ident


async def test_concurrent_create_and_claim(database, ownership):
    jobs = JobService(database, ownership)
    async with database.engine.connect() as connection:
        now = await db_now(connection)

    async def create():
        async with database.transaction() as connection:
            return await jobs.create(
                connection,
                "FORECAST",
                "forecast:fixture",
                {"fixture": True},
                now,
                now + timedelta(seconds=240),
            )

    ids = await asyncio.gather(*(create() for _ in range(20)))
    assert len(set(ids)) == 1
    claims = await asyncio.gather(*(jobs.claim() for _ in range(20)))
    assert len([attempt for attempt in claims if attempt is not None]) == 1
    async with database.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.jobs)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.job_attempts)) == 1


async def test_expired_lease_cannot_revive_or_write(database, ownership):
    jobs = JobService(database, ownership)
    await create_job(database, jobs)
    old = await jobs.claim()
    assert "token=" not in repr(old)
    async with database.transaction() as connection:
        now = await db_now(connection)
        await connection.execute(
            sa.update(s.jobs)
            .where(s.jobs.c.id == old.job_id)
            .values(lease_until=now - timedelta(seconds=1))
        )
    assert not await jobs.renew(old)
    with pytest.raises(StaleAttempt):
        await jobs.authenticate(old.token, "context")
    new = await jobs.claim()
    assert new.fence == old.fence + 1 and new.id != old.id
    with pytest.raises(StaleAttempt):
        async with jobs.transaction(old) as connection:
            await connection.execute(
                sa.update(s.jobs).where(s.jobs.c.id == old.job_id).values(stage="PUBLISHED")
            )
    assert (await jobs.authenticate(new.token, "context")).id == new.id
    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(s.job_attempts.c.token_hash).where(s.job_attempts.c.id == new.id)
            )
            != new.token
        )


async def test_deadline_rechecked_after_row_lock(database, ownership):
    jobs = JobService(database, ownership)
    await create_job(database, jobs, deadline_seconds=0.25)
    attempt = await jobs.claim()
    started = asyncio.Event()

    async def hold_lock():
        async with database.transaction() as connection:
            await connection.execute(
                sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id).with_for_update()
            )
            started.set()
            await asyncio.sleep(0.35)  # Deliberate lock-wait fault injection.

    task = asyncio.create_task(hold_lock())
    await started.wait()
    try:
        with pytest.raises(DomainError) as caught:
            async with jobs.transaction(attempt):
                pytest.fail("A delayed transaction must not cross the deadline")
        assert caught.value.code == "FORECAST_DEADLINE"
    finally:
        await task


async def test_only_one_runtime_and_database_takeover_fences_old_owner(database, ownership):
    jobs = JobService(database, ownership)
    await create_job(database, jobs)
    old = await jobs.claim()
    another = RuntimeOwnership(database)
    with pytest.raises(DomainError) as caught:
        await another.acquire()
    assert caught.value.code == "RUNTIME_ALREADY_RUNNING"
    async with database.transaction() as connection:
        assert await connection.scalar(
            sa.select(sa.func.pg_terminate_backend(ownership.backend_pid))
        )
    assert not await ownership.probe()
    assert not await jobs.renew(old)  # Other pool connections are still alive before takeover.
    await another.acquire()
    try:
        assert another.generation == ownership.generation + 1
        assert not await jobs.renew(old)
        with pytest.raises(StaleAttempt):
            async with jobs.transaction(old):
                pytest.fail("Old pooled connections must lose authority on takeover")
        next_jobs = JobService(database, another)
        new = await next_jobs.claim()
        assert new.fence == old.fence + 1
        assert new.runtime_instance_id == another.instance_id
    finally:
        await another.release()


async def test_completion_revokes_token_and_wrong_kind_is_forbidden(database, ownership):
    jobs = JobService(database, ownership)
    await create_job(database, jobs)
    attempt = await jobs.claim()
    with pytest.raises(DomainError) as caught:
        await jobs.authenticate(attempt.token, "approve")
    assert caught.value.status == 403
    result = uuid4()
    await jobs.finish(attempt, result_id=result)
    with pytest.raises(StaleAttempt):
        await jobs.authenticate(attempt.token, "context")
    async with database.engine.connect() as connection:
        row = (await connection.execute(sa.select(s.jobs))).mappings().one()
        assert row["state"] == "SUCCEEDED" and row["result_id"] == result


async def test_transaction_failure_leaves_no_half_job(database, ownership):
    jobs = JobService(database, ownership)
    with pytest.raises(RuntimeError):
        async with database.transaction() as connection:
            now = await db_now(connection)
            await jobs.create(connection, "EXPLAIN", "fixture:rollback", {}, now)
            raise RuntimeError("injected before COMMIT")
    async with database.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.jobs)) == 0


@pytest.mark.parametrize("expired_deadline", [False, True])
async def test_terminal_claim_revokes_old_attempt_and_clears_lease(
    database, ownership, expired_deadline
):
    jobs = JobService(database, ownership)
    await create_job(database, jobs)
    first = await jobs.claim()
    async with database.transaction() as connection:
        now = await db_now(connection)
        changes = {"lease_until": now - timedelta(seconds=1), "attempts": 2}
        if expired_deadline:
            changes["deadline_at"] = now - timedelta(seconds=1)
        await connection.execute(
            sa.update(s.jobs).where(s.jobs.c.id == first.job_id).values(**changes)
        )
    assert await jobs.claim() is None
    async with database.engine.connect() as connection:
        job = (await connection.execute(sa.select(s.jobs))).mappings().one()
        attempt = (await connection.execute(sa.select(s.job_attempts))).mappings().one()
    assert job["state"] == ("SKIPPED" if expired_deadline else "FAILED")
    assert job["lease_owner"] is job["lease_until"] is None
    assert attempt["token_revoked"] and attempt["status"] == "EXPIRED"
