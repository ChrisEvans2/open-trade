import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from test_analysis_rounds import advance, finish, four, publish, start, tick
from test_analysis_rounds import round_system as round_system
from test_prediction_pipeline import Clock, minute, publish_child, task
from test_prediction_pipeline import prediction_system as prediction_system
from test_structural_runtime import FixtureResources, PublicMarket

from trading_agent.api.auth import CredentialHashes
from trading_agent.config import (
    AppConfig,
    HermesConfig,
    NotificationConfig,
    PredictionConfig,
    RuntimeConfig,
)
from trading_agent.domain.errors import DomainError
from trading_agent.jobs.service import token_hash
from trading_agent.notifications.outbox import Delivery, enqueue
from trading_agent.notifications.routing import prediction_target
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database


def enable_prediction_notices(x):
    config = x.predictions.config.model_copy(
        update={
            "hermes": x.predictions.config.hermes.model_copy(
                update={
                    "controller_profile": "controller",
                    "trading_profile": "prediction",
                    "trading_telegram_enabled": True,
                }
            ),
            "notification": NotificationConfig(
                target="telegram:fixture-controller",
                prediction_target="telegram:fixture-prediction",
            ),
        }
    )
    x.predictions.config = config
    x.rounds.config = config
    return config


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("verified", [True, False])
async def test_runtime_start_notice_requires_successful_start(
    postgres_url, anchor, tmp_path, monkeypatch, verified
):
    clock = Clock(anchor)
    config = AppConfig(
        hermes=HermesConfig(provider="fixture", model="fixture", controller_profile="controller"),
        notification=NotificationConfig(target="telegram:fixture-controller"),
        prediction=PredictionConfig(knowledge_index_path=tmp_path / "index.sqlite"),
        runtime=RuntimeConfig(data_dir=tmp_path / "runtime"),
    )
    sender = SimpleNamespace(send=AsyncMock(return_value=Delivery("SENT", "start-id")))
    runtime = Runtime(
        config,
        Database(postgres_url, clock=clock),
        CredentialHashes(token_hash("fixture-controller"), token_hash("fixture-operator")),
        FixtureResources(),
        PublicMarket(clock),
        AsyncMock(return_value=verified),
        sender=sender,
    )
    # Keep model/execution loops inert: this test checks real startup and delivery only.
    monkeypatch.setattr(runtime, "_loop", AsyncMock(return_value=None))
    if not verified:
        with pytest.raises(DomainError, match="尚未通过校验"):
            await runtime.start()
        check = Database(postgres_url)
        try:
            async with check.engine.connect() as connection:
                assert (
                    await connection.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 0
                )
        finally:
            await check.close()
        sender.send.assert_not_awaited()
        return
    await runtime.start()
    try:
        async with runtime.database.engine.connect() as connection:
            row = (await connection.execute(sa.select(s.outbox))).mappings().one()
            assert row["event_key"] == f"runtime-started:{runtime.ownership.instance_id}"
            assert row["target"] == "telegram:fixture-controller"
            assert "不表示允许开仓" in row["body"]
        await runtime.notifier.tick()
        await runtime.notifier.tick()
        sender.send.assert_awaited_once()
        # Re-entering notification enqueue for this instance still deduplicates.
        async with runtime.database.transaction() as connection:
            await enqueue(
                connection,
                key=f"runtime-started:{runtime.ownership.instance_id}",
                target=row["target"],
                body=row["body"].split("\n[event:")[0],
                now=clock.value,
            )
        await runtime.notifier.tick()
        sender.send.assert_awaited_once()
        # A bounded send may legitimately exceed the old 15s loop-stale cutoff.
        loop_task = asyncio.create_task(asyncio.Event().wait())
        runtime.loops["notifier"].task = loop_task
        runtime.loops["notifier"].last_progress = time.monotonic() - 20
        assert (await runtime.ready())["checks"]["notifier"]
        runtime.loops["notifier"].last_progress = time.monotonic() - 55
        assert not (await runtime.ready())["checks"]["notifier"]
    finally:
        await runtime.stop()


async def test_recovery_only_publishes_parent_summary_and_errors_remain_atomic(
    round_system, monkeypatch
):
    x = round_system
    config = enable_prediction_notices(x)
    main = await start(x)
    await four(x, main)
    await publish(x, main, await x.rounds.begin(main, "parent"))
    await finish(x, main)
    async with x.db.transaction() as c:
        notices = (await c.execute(sa.select(s.outbox))).mappings().all()
        assert len(notices) == 1  # one parent summary, no individual child success spam
        assert notices[0]["target"] == prediction_target(config)
        assert notices[0]["event_key"].startswith("prediction-published:")
        await x.rounds.event(c, x.engine, "BTCUSDT", [], "PARENT_ENDED", "ended", x.clock.value)
    await advance(x, 30)
    await tick(x)
    main = await x.rounds.start(await x.jobs.claim(("PREDICT_PARENT",)))
    await x.rounds.failed(main, DomainError("AGENT_TIMEOUT", "fixture", 503))
    await x.jobs.release_model_slot(main, cleaned=True)
    async with x.db.engine.connect() as c:
        group = await c.scalar(sa.select(s.analysis_scopes.c.error_group))
        assert group is not None
    await advance(x, 60)
    await tick(x)
    main = await x.rounds.start(await x.jobs.claim(("PREDICT_PARENT",)))
    stage = await x.rounds.begin(main, "parent")
    original = enqueue

    async def fail_after_enqueue(connection, **kwargs):
        await original(connection, **kwargs)
        raise RuntimeError("fixture rollback after notification insert")

    monkeypatch.setattr("trading_agent.notifications.predictions.enqueue", fail_after_enqueue)
    with pytest.raises(RuntimeError, match="fixture rollback"):
        await publish(x, main, stage)
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 5
        assert not await x.rounds.parent_available(c, x.engine, "BTCUSDT")
        assert (
            await c.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 2
        )  # initial summary + error; the new parent summary rolled back
        assert await c.scalar(sa.select(s.analysis_scopes.c.error_group)) == group
    monkeypatch.setattr("trading_agent.notifications.predictions.enqueue", original)
    await publish(x, main, stage)
    with pytest.raises(DomainError, match="轮次已经结束"):
        await publish(x, main, stage)  # a finished round cannot duplicate recovery notices
    async with x.db.engine.connect() as c:
        notices = (
            (
                await c.execute(
                    sa.select(s.outbox).where(s.outbox.c.event_key == f"analysis-recovered:{group}")
                )
            )
            .mappings()
            .all()
        )
        assert notices == []
        all_notices = (await c.execute(sa.select(s.outbox))).mappings().all()
        assert len(all_notices) == 3  # two parent summaries and one error
        assert all(n["target"] == prediction_target(config) for n in all_notices)
        assert sum(n["event_key"].startswith("prediction-published:") for n in all_notices) == 2
        assert await x.rounds.parent_available(c, x.engine, "BTCUSDT")
        assert await c.scalar(sa.select(s.analysis_scopes.c.error_group)) is None


@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.parametrize("outcome", ["EXPIRED", "ACHIEVED"])
async def test_prediction_end_is_silent_but_verification_continues(round_system, parent, outcome):
    from datetime import timedelta

    x = round_system
    enable_prediction_notices(x)
    if parent:
        main = await start(x)
        await four(x, main)
        await publish(x, main, await x.rounds.begin(main, "parent"))
        ident = await x.rounds.result(main)
        await finish(x, main)
    else:
        ident = await publish_child((x.db, x.clock, x.jobs, x.predictions, x.engine))
    async with x.db.engine.connect() as c:
        fact = (
            (await c.execute(sa.select(s.prediction_facts).where(s.prediction_facts.c.id == ident)))
            .mappings()
            .one()
        )
        old_notices = (await c.execute(sa.select(s.outbox))).mappings().all()
    published = fact["body"]["prediction"]
    from datetime import datetime

    begin = datetime.fromisoformat(published["observation_start"])
    expiry = datetime.fromisoformat(published["expires_at"])
    x.clock.value = expiry + timedelta(minutes=1)
    attempt = await task(
        (x.db, x.clock, x.jobs, x.predictions, x.engine), kind="PREDICT_VERIFY", freeze=False
    )
    # Stay between trigger and invalidation until the original expiry.
    minutes = tuple(
        minute("BTC-USDT-SWAP", begin + timedelta(minutes=n), high="179", low="179", close="179")
        for n in range(int((expiry - begin).total_seconds() // 60))
    )
    if outcome == "ACHIEVED":
        minutes = (
            minute("BTC-USDT-SWAP", begin, high="190", low="190", close="190"),
            minute(
                "BTC-USDT-SWAP", begin + timedelta(minutes=1), high="205", low="190", close="201"
            ),
        ) + minutes[2:]
    first = await x.predictions.verify(attempt, ident, minutes)
    assert first.state.lifecycle == outcome
    await x.predictions.verify(attempt, ident, minutes)
    async with x.db.engine.connect() as c:
        notices = (await c.execute(sa.select(s.outbox))).mappings().all()
        assert notices == old_notices
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_evaluations)) > 0
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.trade_intents)) == 0
