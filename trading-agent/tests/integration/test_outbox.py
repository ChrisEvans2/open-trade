from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import publish_child, task

from trading_agent.domain.errors import DomainError
from trading_agent.notifications.outbox import Delivery, Notifier, enqueue
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def test_outbox_event_dedupe_expired_claim_is_unknown_and_never_resent(prediction_system):
    database, clock, jobs, predictions, _ = prediction_system
    notifier = Notifier(database, jobs.ownership, None, predictions.config)
    async with database.transaction() as connection:
        ident = await enqueue(
            connection,
            key="fixture-event",
            target="fixture-target",
            body="fixture",
            now=clock.value,
        )
        assert (
            await enqueue(
                connection,
                key="fixture-event",
                target="fixture-target",
                body="fixture",
                now=clock.value,
            )
            == ident
        )
    first = (await notifier.claim())[0]
    clock.value += timedelta(seconds=121)
    assert await notifier.claim() == []
    assert not await notifier.acknowledge(first, Delivery("SENT", "late-old-id"))
    async with database.engine.connect() as connection:
        row = (await connection.execute(sa.select(s.outbox))).mappings().one()
        assert row["status"] == "UNKNOWN" and row["attempts"] == 1
        assert row["last_error"] == "PRIOR_DELIVERY_UNKNOWN"
        assert row["claim_id"] is None and row["lease_until"] is None
        assert row["transport_message_id"] is None and "[event:" in row["body"]
    clock.value += timedelta(days=1)
    assert await notifier.claim() == []


async def test_confirmed_failure_retries_but_unknown_stops_even_after_restart(prediction_system):
    database, clock, jobs, predictions, _ = prediction_system
    notifier = Notifier(database, jobs.ownership, None, predictions.config)
    async with database.transaction() as connection:
        await enqueue(
            connection, key="confirmed-failure", target="fixture", body="fixture", now=clock.value
        )
    for number, delay in ((1, 60), (2, 300), (3, 900), (4, 900), (5, 900)):
        claim = (await notifier.claim())[0]
        assert claim["attempts"] == number
        await notifier.acknowledge(claim, Delivery("FAILED", error_code="fixture-rejected"))
        clock.value += timedelta(seconds=delay)
    assert await notifier.claim() == []
    async with database.engine.connect() as connection:
        row = (await connection.execute(sa.select(s.outbox))).mappings().one()
        assert row["status"] == "FAILED" and row["attempts"] == 5
    async with database.transaction() as connection:
        await enqueue(
            connection, key="unknown-send", target="fixture", body="fixture", now=clock.value
        )
    claim = (await notifier.claim())[0]
    await notifier.acknowledge(claim, Delivery("UNKNOWN", error_code="SEND_TIMEOUT"))
    clock.value += timedelta(days=1)
    restarted = Notifier(database, jobs.ownership, None, predictions.config)
    assert await restarted.claim() == []
    async with database.engine.connect() as connection:
        row = (
            (await connection.execute(sa.select(s.outbox).where(s.outbox.c.id == claim["id"])))
            .mappings()
            .one()
        )
        assert row["status"] == "UNKNOWN" and row["attempts"] == 1
        assert row["last_error"] == "SEND_TIMEOUT"


@pytest.mark.parametrize("kind", ["PREDICT_PARENT", "PREDICT_TIMEFRAME"])
@pytest.mark.parametrize("code", ["CONTEXT_EXPIRED", "CONTEXT_CHANGED", "POSITION_CHANGED"])
async def test_context_refresh_keeps_failure_and_new_job_without_notification(
    prediction_system, kind, code
):
    database, clock, jobs, predictions, engine = prediction_system
    config = predictions.config.model_copy(
        update={"notification": predictions.config.notification.model_copy(update={"target": "tg"})}
    )
    runtime = SimpleNamespace(
        runner=SimpleNamespace(_result=AsyncMock(return_value=None)),
        database=database,
        ownership=jobs.ownership,
        jobs=jobs,
        predictions=predictions,
        engine_id=engine,
        config=config,
    )
    if kind == "PREDICT_PARENT":
        await publish_child(prediction_system)
    attempt = await task(prediction_system, kind=kind)
    await Runtime._failure(runtime, attempt, DomainError(code, "fixture", 409))
    async with database.engine.connect() as connection:
        row = (
            (await connection.execute(sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id)))
            .mappings()
            .one()
        )
        assert row["state"] == "FAILED" and row["last_error"] == {"code": code}
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 0
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(s.jobs)
                .where(
                    s.jobs.c.kind == kind, s.jobs.c.state == "QUEUED", s.jobs.c.id != attempt.job_id
                )
            )
            == 1
        )
    # Genuine terminal errors still notify through the same runtime path.
    attempt = await jobs.claim((kind,))
    await Runtime._failure(runtime, attempt, DomainError("FIXTURE_REAL_FAILURE", "fixture", 422))
    async with database.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 1


async def test_old_context_notices_retire_without_sending_or_deleting_history(prediction_system):
    database, clock, jobs, predictions, _ = prediction_system
    obsolete_ids = []
    async with database.transaction() as connection:
        for number, status in enumerate(("PENDING", "RETRY_WAIT", "UNKNOWN", "SENDING")):
            job = await jobs.create(
                connection, "PREDICT_PARENT", f"old-job:{number}", {}, clock.value
            )
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == job)
                .values(state="FAILED", last_error={"code": "CONTEXT_EXPIRED"})
            )
            ident = await enqueue(
                connection,
                key=f"job-failed:{job}:CONTEXT_EXPIRED",
                target="fixture",
                body="original notice",
                now=clock.value,
            )
            obsolete_ids.append(ident)
            await connection.execute(
                sa.update(s.outbox)
                .where(s.outbox.c.id == ident)
                .values(
                    status=status,
                    attempts=number,
                    lease_until=clock.value - timedelta(seconds=1) if status == "SENDING" else None,
                )
            )
        ordinary = await enqueue(
            connection, key="execution:real-event", target="fixture", body="fill", now=clock.value
        )
    notifier = Notifier(database, jobs.ownership, None, predictions.config)
    claims = await notifier.claim()
    assert [claim["id"] for claim in claims] == [ordinary]
    assert await notifier.acknowledge(claims[0], Delivery("SENT", "fixture-id"))
    async with database.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(s.outbox)
                    .where(s.outbox.c.id.in_(obsolete_ids))
                    .order_by(s.outbox.c.attempts)
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 4
        assert [row["attempts"] for row in rows] == [0, 1, 2, 3]
        assert all(row["status"] == "FAILED" for row in rows)
        assert all(row["last_error"] == "NOTIFICATION_SUPERSEDED" for row in rows)
        assert all(row["body"].startswith("original notice\n[event:") for row in rows)


async def test_notification_failure_does_not_rollback_business_transaction(prediction_system):
    database, clock, jobs, predictions, _ = prediction_system

    class FailureSender:
        async def send(self, target, body):
            return Delivery("FAILED", error_code="fixture-network")

    notifier = Notifier(database, jobs.ownership, FailureSender(), predictions.config)
    async with database.transaction() as connection:
        await jobs.create(connection, "KNOWLEDGE_INDEX", "fixture-business", {}, clock.value)
        await enqueue(
            connection, key="fixture-committed", target="fixture", body="已保存", now=clock.value
        )
    await notifier.tick()
    async with database.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.jobs)) == 1
        assert await connection.scalar(sa.select(s.outbox.c.status)) == "RETRY_WAIT"


async def test_delivered_message_with_lost_receipt_is_not_sent_again(prediction_system):
    database, clock, jobs, predictions, _ = prediction_system

    class LostReceiptSender:
        def __init__(self):
            self.delivered = []

        async def send(self, target, body):
            self.delivered.append((target, body))  # remote accepted before receipt was lost
            raise TimeoutError

    sender = LostReceiptSender()
    async with database.transaction() as connection:
        await enqueue(
            connection, key="lost-receipt", target="fixture", body="fixture", now=clock.value
        )
    await Notifier(database, jobs.ownership, sender, predictions.config).tick()
    clock.value += timedelta(days=1)
    await Notifier(database, jobs.ownership, sender, predictions.config).tick()
    assert len(sender.delivered) == 1
    async with database.engine.connect() as connection:
        row = (await connection.execute(sa.select(s.outbox))).mappings().one()
        assert row["status"] == "UNKNOWN" and row["attempts"] == 1
        assert row["transport_message_id"] is None
