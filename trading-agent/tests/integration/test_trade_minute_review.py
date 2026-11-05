"""Independent real-PG tests of bounded fairness and historical revision visibility."""

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.config import MarketConfig, TradeMinuteConfig
from trading_agent.market.models import Observation
from trading_agent.market.okx import OKXClient
from trading_agent.market.trade_collector import TradeMinuteCollector
from trading_agent.market.trades import MINUTE_SOURCE
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_observation, persist_observations
from trading_agent.storage.permissions import provision_app_permissions, verify_app_permissions
from trading_agent.storage.trades import trade_windows

pytestmark = pytest.mark.postgres
BTC, ETH = "BTC-USDT-SWAP", "ETH-USDT-SWAP"


class QuietMinuteSource:
    parse_bar = OKXClient.parse_bar

    def __init__(self, system, config):
        self.db, self.clock, *_ = system
        self.config = config

    def now(self):
        return self.clock.value

    async def get(self, path, params, deadline):
        assert self.db.engine.pool.checkedout() == 1
        if path.endswith("/time"):
            return [{"ts": str(int(self.now().timestamp() * 1000))}]
        inst = params["instId"]
        if path.endswith("/instruments"):
            return [
                dict(
                    instId=inst,
                    instType="SWAP",
                    ctType="linear",
                    settleCcy="USDT",
                    ctValCcy=inst.split("-")[0],
                    ctVal="0.01",
                    ctMult="1",
                )
            ]
        start_ms = int(params["after"]) - 60_000
        if path.endswith("history-candles"):
            return [[str(start_ms), "100", "100", "100", "100", "0", "0", "0", "1"]]
        return [
            dict(
                instId=inst,
                ts=str(start_ms - 1),
                tradeId=str(start_ms),
                sz="1",
                px="100",
                side="buy",
                source="0",
            )
        ]


def collector(system, *, max_minutes=2):
    db, _, jobs, *_ = system
    config = MarketConfig(
        inst_id=BTC,
        contract_type="perpetual",
        settlement_asset="USDT",
        trade_minutes=TradeMinuteConfig(
            bootstrap_days=1, max_minutes_per_cycle=max_minutes, max_requests_per_cycle=5
        ),
    )
    source = QuietMinuteSource(system, config)
    return TradeMinuteCollector(db, jobs.ownership, source, config, inst_ids=(BTC, ETH))


async def test_tight_cycle_budget_does_not_permanently_favor_first_asset(prediction_system):
    db, *_ = prediction_system
    worker = collector(prediction_system)
    first = await worker.tick()
    second = await worker.tick()
    await worker.tick()
    await worker.tick()
    assert first["requests"] <= 5 and second["requests"] <= 5
    async with db.engine.connect() as connection:
        verified = (
            (
                await connection.execute(
                    sa.select(s.trade_minute_work.c.inst_id).where(
                        s.trade_minute_work.c.state == "VERIFIED"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(verified) == {BTC, ETH}


async def test_due_audit_rotates_into_older_history_before_bootstrap(prediction_system):
    db, clock, *_ = prediction_system
    worker = collector(prediction_system, max_minutes=4)
    await worker.plan()
    latest = clock.value.replace(second=0, microsecond=0) - timedelta(minutes=2)
    old, recent = latest - timedelta(hours=12), latest - timedelta(minutes=10)
    async with db.transaction() as connection:
        for opened, updated in (
            (old, clock.value - timedelta(hours=6)),
            (recent, clock.value - timedelta(minutes=20)),
        ):
            await connection.execute(
                sa.update(s.trade_minute_work)
                .where(
                    s.trade_minute_work.c.inst_id == BTC, s.trade_minute_work.c.open_at == opened
                )
                .values(state="VERIFIED", updated_at=updated)
            )
    await worker.plan()
    plan = await worker.plan()  # Every third cycle reserves first turn for a due audit.
    audit_items = [(item, audit) for item, audit in plan if audit and item["inst_id"] == BTC]
    assert len(audit_items) == 1
    assert audit_items[0][0]["open_at"] == old
    assert plan[0][1] is True  # A due audit must not always sit behind budget-consuming bootstrap.


async def test_new_invalid_revision_hides_old_good_minute_only_after_its_availability(
    prediction_system,
):
    db, clock, jobs, *_ = prediction_system
    end = clock.value.replace(minute=0, second=0, microsecond=0)
    observed_at = clock.value
    values = tuple(
        Observation(
            source=MINUTE_SOURCE,
            inst_id=BTC,
            kind="taker_volume",
            unit="base_quantity",
            event_at=end - timedelta(minutes=59 - i),
            available_at=observed_at,
            coverage_start=end - timedelta(minutes=60 - i),
            coverage_end=end - timedelta(minutes=59 - i),
            payload={"buy": "2", "sell": "1", "timeframe": "1m"},
        )
        for i in range(60)
    )
    async with db.transaction() as connection:
        await jobs.ownership.guard(connection)
        stored = await persist_observations(connection, values)
        before = await trade_windows(connection, BTC, "1h", end, observed_at)
    clock.value += timedelta(seconds=20)
    invalid = stored[-1].model_copy(
        update={
            "available_at": clock.value,
            "quality": "MISSING",
            "payload": {"reason": "TRADE_SIDE_CHECK_FAILED", "timeframe": "1m"},
        }
    )
    async with db.transaction() as connection:
        await jobs.ownership.guard(connection)
        await persist_observation(connection, invalid)
        old_asof = await trade_windows(connection, BTC, "1h", end, observed_at)
        new_asof = await trade_windows(connection, BTC, "1h", end, clock.value)
    assert before[-1].quality == old_asof[-1].quality == "OK"
    assert (
        old_asof[-1].payload["minute_versions_hash"] == before[-1].payload["minute_versions_hash"]
    )
    assert new_asof[-1].quality == "MISSING"
    assert new_asof[-1].payload["verified_minutes"] == 59
    assert "buy" not in new_asof[-1].payload
    assert all(window.quality == "MISSING" for window in before[:-1])


async def test_minimum_runtime_role_can_expire_only_disposable_trade_cache(database):
    role = "trade_review_" + uuid4().hex[:16]
    try:
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
            await provision_app_permissions(connection, role)
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'SET LOCAL ROLE "{role}"'))
            assert await verify_app_permissions(connection)
            # Even deletion from an empty cache needs the privilege at production startup.
            await connection.execute(sa.delete(s.trade_raw_cache))
            for table in (
                s.market_observations,
                s.market_bars,
                s.prediction_inputs,
                s.trade_minute_work,
            ):
                assert not await connection.scalar(
                    sa.text("SELECT has_table_privilege(:table_name, 'DELETE')"),
                    {"table_name": table.name},
                )
    finally:
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'DROP OWNED BY "{role}"'))
            await connection.execute(sa.text(f'DROP ROLE "{role}"'))
