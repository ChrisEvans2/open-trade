import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_analysis_rounds import round_system as round_system
from test_analysis_rounds import start
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import task

from trading_agent.market.models import Observation
from trading_agent.prediction.scheduler import PredictionScheduler
from trading_agent.storage import schema as s
from trading_agent.storage.market import (
    persist_bar,
    persist_bars,
    persist_observation,
    persist_observations,
)

pytestmark = pytest.mark.postgres


async def test_concurrent_window_and_single_bar_writers_keep_revisions_and_rollback(
    database, anchor
):
    bars = bars_at(anchor)

    async def store(window):
        async with database.transaction() as c:
            return await persist_bars(c, window)

    copies = await asyncio.gather(*(store(bars) for _ in range(8)))
    assert all([b.id for b in copy] == [b.id for b in copies[0]] for copy in copies)
    changed = bars[0].model_copy(update={"id": uuid4(), "volume_contracts": Decimal(999)})

    async def single():
        async with database.transaction() as c:
            return await persist_bar(c, changed)

    one, many = await asyncio.gather(single(), store((changed, *bars[1:])))
    assert one.id == many[0].id and one.revision_no == many[0].revision_no == 2
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction() as c:
            await persist_bars(c, (changed.model_copy(update={"volume_contracts": Decimal(1000)}),))
            raise RuntimeError("rollback")
    async with database.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.market_bars)) == 81
    assert (await store((changed,)))[0].id == one.id


async def test_external_history_batch_keeps_concurrent_revisions_duplicates_and_rollback(
    database, anchor
):
    observations = tuple(
        Observation(
            source="history-fixture",
            inst_id="BTC-USDT-SWAP",
            kind="oi",
            event_at=anchor - timedelta(minutes=i),
            available_at=anchor,
            unit="contracts",
            payload={"oi": str(100 + i)},
        )
        for i in range(114)
    )

    async def store(items):
        async with database.transaction() as c:
            return await persist_observations(c, items)

    copies = await asyncio.gather(*(store(observations) for _ in range(8)))
    assert all([o.id for o in copy] == [o.id for o in copies[0]] for copy in copies)
    changed = observations[0].model_copy(update={"id": uuid4(), "payload": {"oi": "999"}})

    async def single():
        async with database.transaction() as c:
            return await persist_observation(c, changed)

    one, many = await asyncio.gather(single(), store((changed, *observations[1:])))
    assert one.id == many[0].id and one.revision_no == 2
    third = changed.model_copy(update={"payload": {"oi": "1000"}})
    versions = await store((changed, third, third))
    assert [o.revision_no for o in versions] == [2, 3, 3]
    assert versions[1].id == versions[2].id
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction() as c:
            await persist_observations(c, (third.model_copy(update={"payload": {"oi": "1001"}}),))
            raise RuntimeError("rollback")
    async with database.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.market_observations)) == 116
    assert (await store((third,)))[0].id == versions[1].id


async def test_scheduler_does_not_block_unrelated_task_renewal_behind_global_fence(
    prediction_system,
):
    db, _, jobs, predictions, engine = prediction_system
    attempt = await task(prediction_system, freeze=False)
    scheduler = PredictionScheduler(jobs, predictions.config, engine, predictions)
    async with db.transaction() as c:
        # An unrelated business writer holds SHARE for longer than a tick.
        await jobs.ownership.guard(c)
        tick = asyncio.create_task(scheduler.tick())
        try:
            await asyncio.sleep(0.03)
            assert await asyncio.wait_for(jobs.renew(attempt), 0.7)
            await asyncio.wait_for(tick, 0.7)
        finally:
            if not tick.done():
                tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)


async def test_coordinator_renewal_during_freeze_with_114_external_observations(round_system):
    x = round_system
    main = await start(x)

    async def observations(deadline, **scope):
        return tuple(
            Observation(
                source="history-fixture",
                inst_id="BTC-USDT-SWAP",
                kind="oi",
                event_at=x.clock.value - timedelta(minutes=i + 1),
                available_at=x.clock.value,
                unit="contracts",
                payload={"oi": str(100 + i)},
            )
            for i in range(114)
        )

    x.market.observations = observations
    preparing = asyncio.create_task(x.rounds.begin(main, "1h"))
    try:
        await asyncio.sleep(0.03)
        assert await asyncio.wait_for(x.jobs.renew(main), 0.7)
        stage = await asyncio.wait_for(preparing, 10)
        assert (
            sum(ref.startswith("observation.") for ref in stage["context"]["evidence_options"])
            == 114 + 20  # The minute source also freezes 20 explicit cold-start windows.
        )
        async with x.db.engine.connect() as c:
            assert (
                await c.scalar(
                    sa.select(sa.func.count())
                    .select_from(s.market_observations)
                    .where(s.market_observations.c.source == "history-fixture")
                )
                == 114
            )
    finally:
        if not preparing.done():
            preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)
