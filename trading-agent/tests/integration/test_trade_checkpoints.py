from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.config import MarketConfig, TradeMinuteConfig
from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.market.models import Observation
from trading_agent.market.okx import OKXClient
from trading_agent.market.trade_collector import TradeMinuteCollector
from trading_agent.market.trades import MINUTE_SOURCE
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_observation, persist_observations
from trading_agent.storage.trade_checkpoints import load_checkpoint
from trading_agent.storage.trades import coverage_status

pytestmark = pytest.mark.postgres
BTC, ETH = "BTC-USDT-SWAP", "ETH-USDT-SWAP"


class BusySource:
    parse_bar = OKXClient.parse_bar

    def __init__(self, system, config):
        self.db, self.clock, *_ = system
        self.config = config
        self.calls = []
        self.fail = False
        self.request_seconds = 0

    def now(self):
        return self.clock.value

    async def get(self, path, params, deadline):
        assert self.db.engine.pool.checkedout() == 1  # Only runtime ownership holds a connection.
        if self.now() >= deadline:
            raise DomainError("MARKET_DEADLINE", "fixture slice exhausted")
        self.clock.value += timedelta(seconds=self.request_seconds)
        self.calls.append((path, dict(params)))
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
        cursor = int(params["after"])
        if path.endswith("history-candles"):
            return [[str(cursor - 60000), "100", "100", "100", "100", "4399", "43.99", "4399", "1"]]
        if self.fail:
            raise DomainError("MARKET_RATE_LIMIT", "fixture limit")
        if params["type"] == "2":
            start, top = cursor - 60000, 4399
        else:
            start, top = cursor // 10000, cursor % 10000 - 1
        return [
            dict(
                instId=inst,
                tradeId=str(start * 10000 + i),
                ts=str(start + i * 60000 // 4400 if i else start - 1),
                sz="1",
                px="100",
                side="buy" if i % 2 else "sell",
                source="0",
            )
            for i in range(top, max(-1, top - 100), -1)
        ]


def worker_for(system, **settings):
    db, _, jobs, *_ = system
    config = MarketConfig(trade_minutes=TradeMinuteConfig(bootstrap_days=1, **settings))
    source = BusySource(system, config)
    return TradeMinuteCollector(db, jobs.ownership, source, config, inst_ids=(BTC, ETH)), source


async def pin_minute(worker, start, inst=BTC):
    async def one():
        return [({"inst_id": inst, "open_at": start}, False)]

    worker.plan = one


async def test_44_pages_resume_across_process_restarts_and_expired_raw_retention(prediction_system):
    db, clock, *_ = prediction_system
    worker, source = worker_for(prediction_system)
    plan = await worker.plan()
    start = plan[0][0]["open_at"]
    await pin_minute(worker, start)
    first = await worker.tick()
    assert first["paused"] == 1 and first["incomplete"] == 0 and first["pages"] == 10
    async with db.transaction() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.market_observations)
                .where(s.market_observations.c.source == MINUTE_SOURCE)
            )
            == 0
        )
        work = (
            (
                await conn.execute(
                    sa.select(s.trade_minute_work).where(
                        s.trade_minute_work.c.inst_id == BTC, s.trade_minute_work.c.open_at == start
                    )
                )
            )
            .mappings()
            .one()
        )
        assert work["state"] == "PENDING" and work["attempts"] == 0
    saved = await load_checkpoint(db, BTC, start)
    assert saved["pages"] == 10
    clock.value += timedelta(hours=25)
    restarted, source = worker_for(prediction_system)
    await restarted.plan()  # Prune raw cache, but retain in-flight pages beyond 24 hours.
    assert (await load_checkpoint(db, BTC, start))["pages"] == 10
    await pin_minute(restarted, start)
    for _ in range(4):
        result = await restarted.tick()
    assert result["verified"] == 1 and result["pages"] == 4
    calls = [params for path, params in source.calls if path.endswith("history-trades")]
    assert len(calls) == 34 and calls[0]["after"] == str(saved["cursor"])
    assert all(p["type"] == "1" for p in calls)
    assert await load_checkpoint(db, BTC, start) is None
    async with db.transaction() as conn:
        row = (
            (
                await conn.execute(
                    sa.select(s.market_observations).where(
                        s.market_observations.c.source == MINUTE_SOURCE
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["quality"] == "OK"
        assert Decimal(row["payload"]["buy"]) == Decimal("22")
        assert Decimal(row["payload"]["sell"]) == Decimal("21.99")
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_minute_pages)) == 0


async def test_network_failure_keeps_cursor_and_resumes_after_backoff(prediction_system):
    db, _, *_ = prediction_system
    worker, source = worker_for(prediction_system)
    start = (await worker.plan())[0][0]["open_at"]
    await pin_minute(worker, start)
    await worker.tick()
    old = await load_checkpoint(db, BTC, start)
    source.fail = True
    assert (await worker.tick())["incomplete"] == 1
    assert (await load_checkpoint(db, BTC, start))["cursor"] == old["cursor"]
    source.fail = False
    await worker.tick()
    assert (await load_checkpoint(db, BTC, start))["pages"] == 20


async def test_stale_owner_cannot_save_page_or_advance_checkpoint(prediction_system):
    db, _, jobs, *_ = prediction_system
    worker, source = worker_for(prediction_system)
    start = (await worker.plan())[0][0]["open_at"]
    await pin_minute(worker, start)
    await worker.tick()
    saved = await load_checkpoint(db, BTC, start)
    original = source.get

    async def lose_owner(path, params, deadline):
        value = await original(path, params, deadline)
        if path.endswith("history-trades"):
            await jobs.ownership.release()
        return value

    source.get = lose_owner
    with pytest.raises(StaleAttempt):
        await worker.tick()
    assert (await load_checkpoint(db, BTC, start))["cursor"] == saved["cursor"]


async def test_small_budget_rotates_all_asset_and_lane_pairs(prediction_system):
    db, clock, *_ = prediction_system
    worker, _ = worker_for(
        prediction_system, max_requests_per_cycle=5, max_minutes_per_cycle=2, pages_per_turn=2
    )
    for _ in range(4):
        await worker.tick()
    async with db.transaction() as conn:
        rows = (await conn.execute(sa.select(s.trade_minute_checkpoints))).mappings().all()
    latest = clock.value.replace(second=0, microsecond=0) - timedelta(minutes=2)
    pairs = {
        (r["inst_id"], "live" if r["open_at"] >= latest - timedelta(minutes=2) else "history")
        for r in rows
    }
    assert pairs == {(inst, lane) for inst in (BTC, ETH) for lane in ("live", "history")}


async def test_coverage_status_uses_each_period_close_boundary(prediction_system):
    db, clock, *_ = prediction_system
    async with db.transaction() as conn:
        report = await coverage_status(conn, (BTC,), clock.value)
    periods = report["instruments"][BTC]["periods"]
    assert periods["1h"]["end"].endswith("08:00:00+00:00")
    assert periods["12h"]["end"].endswith("00:00:00+00:00")
    assert periods["12h"]["expected_minutes"] == 14400
    assert not periods["12h"]["ready"]


async def test_legacy_budget_failures_requeue_without_resetting_real_error_backoff(
    prediction_system,
):
    db, clock, *_ = prediction_system
    worker, _ = worker_for(prediction_system)
    plan = await worker.plan()
    start = plan[0][0]["open_at"]
    async with db.transaction() as conn:
        for inst, reason in ((BTC, "TRADE_CYCLE_BUDGET"), (ETH, "MARKET_RATE_LIMIT")):
            await conn.execute(
                sa.update(s.trade_minute_work)
                .where(
                    s.trade_minute_work.c.inst_id == inst, s.trade_minute_work.c.open_at == start
                )
                .values(
                    state="ERROR",
                    attempts=7,
                    reason=reason,
                    next_retry_at=clock.value + timedelta(hours=1),
                )
            )
    await worker.plan()
    async with db.transaction() as conn:
        records = (
            (
                await conn.execute(
                    sa.select(s.trade_minute_work).where(s.trade_minute_work.c.open_at == start)
                )
            )
            .mappings()
            .all()
        )
    records = {r["inst_id"]: r for r in records}
    assert records[BTC]["state"] == "PENDING" and records[BTC]["attempts"] == 0
    assert records[BTC]["next_retry_at"] == clock.value
    assert records[ETH]["state"] == "ERROR" and records[ETH]["attempts"] == 7


async def test_arriving_minutes_and_history_both_progress_with_44_page_source(prediction_system):
    db, clock, *_ = prediction_system
    worker, source = worker_for(prediction_system)
    start = clock.value.replace(second=0, microsecond=0)
    source.request_seconds = 0.12
    for _ in range(12):
        result = await worker.tick()
        assert result["incomplete"] == 0 and result["requests"] <= 300
        clock.value += timedelta(seconds=worker.settings.busy_poll_seconds)
    async with db.transaction() as conn:
        verified = (
            (
                await conn.execute(
                    sa.select(s.trade_minute_work).where(s.trade_minute_work.c.state == "VERIFIED")
                )
            )
            .mappings()
            .all()
        )
    for inst in (BTC, ETH):
        assert any(r["inst_id"] == inst and r["open_at"] >= start for r in verified)
        assert any(
            r["inst_id"] == inst and r["open_at"] < start - timedelta(minutes=3) for r in verified
        )


async def test_full_ten_day_window_and_one_invalid_minute_change_readiness(prediction_system):
    from trading_agent.domain.time import boundary
    from trading_agent.storage.trades import trade_windows

    db, clock, jobs, *_ = prediction_system
    end = boundary(clock.value, "12h")
    items = [
        Observation(
            source=MINUTE_SOURCE,
            inst_id=BTC,
            kind="taker_volume",
            event_at=end - timedelta(minutes=i),
            available_at=clock.value,
            coverage_start=end - timedelta(minutes=i + 1),
            coverage_end=end - timedelta(minutes=i),
            unit="base_quantity",
            payload={"buy": "2", "sell": "1", "timeframe": "1m"},
        )
        for i in range(14400)
    ]
    # Like the collector, release per-minute locks between bounded write transactions.
    for offset in range(0, len(items), 500):
        async with db.transaction() as conn:
            await jobs.ownership.guard(conn)
            await persist_observations(conn, items[offset : offset + 500])
    async with db.transaction() as conn:
        windows = await trade_windows(conn, BTC, "12h", end, clock.value)
    assert len(windows) == 20 and all(w.quality == "OK" for w in windows)
    assert sum(w.payload["buy"] - w.payload["sell"] for w in windows) == 14400
    original_time = clock.value
    clock.value += timedelta(seconds=1)
    async with db.transaction() as conn:
        await jobs.ownership.guard(conn)
        await persist_observation(
            conn,
            items[0].model_copy(
                update={
                    "available_at": clock.value,
                    "quality": "MISSING",
                    "payload": {"reason": "TRADE_CANDLE_VOLUME_MISMATCH", "timeframe": "1m"},
                }
            ),
        )
        old = await trade_windows(conn, BTC, "12h", end, original_time)
        status = await coverage_status(conn, (BTC,), clock.value)
    assert all(w.quality == "OK" for w in old)
    period = status["instruments"][BTC]["periods"]["12h"]
    assert period["verified_minutes"] == 14399 and period["missing_minutes"] == 1
    assert not period["ready"]
