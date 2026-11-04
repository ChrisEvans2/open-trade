from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import task

from trading_agent.config import MarketConfig, TradeMinuteConfig
from trading_agent.domain.errors import StaleAttempt
from trading_agent.market.models import Observation
from trading_agent.market.okx import OKXClient
from trading_agent.market.trade_collector import TradeMinuteCollector
from trading_agent.market.trades import MINUTE_SOURCE, capture_minute
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_observation, persist_observations
from trading_agent.storage.trades import trade_windows

pytestmark = pytest.mark.postgres
INST = "BTC-USDT-SWAP"


def metadata(inst=INST):
    return dict(
        instId=inst,
        instType="SWAP",
        ctType="linear",
        settleCcy="USDT",
        ctValCcy=inst.split("-")[0],
        ctVal="0.01",
        ctMult="1",
    )


class PublicClient:
    parse_bar = OKXClient.parse_bar

    def __init__(self, db, clock, config):
        self.db, self.clock, self.config = db, clock, config
        self.calls = []
        self.mismatch = False
        self.reverse = False

    def now(self):
        return self.clock()

    async def get(self, path, params, deadline):
        # Runtime ownership is the only connection allowed during network I/O.
        assert self.db.engine.pool.checkedout() == 1
        self.calls.append(path)
        if path.endswith("/time"):
            return [{"ts": str(int(self.now().timestamp() * 1000))}]
        if path.endswith("/instruments"):
            return [metadata(params["instId"])]
        end = int(params["after"])
        start = end - 60000
        if path.endswith("history-candles"):
            volume = "6" if self.mismatch else "5"
            return [
                [
                    str(start),
                    "100",
                    "100",
                    "100",
                    "100",
                    str(Decimal(volume) * 100),
                    volume,
                    str(Decimal(volume) * 100),
                    "1",
                ]
            ]
        assert path.endswith("history-trades") and params["type"] == "2"
        return [
            dict(
                instId=params["instId"],
                tradeId=str(start * 10 + i),
                ts=str(start + delta),
                sz=size,
                side=side,
                source="0",
                px="100",
            )
            for i, delta, size, side in [
                (3, 40000, "300", "buy" if self.reverse else "sell"),
                (2, 1000, "200", "sell" if self.reverse else "buy"),
                (1, -1, "100", "buy"),
            ]
        ]


def collector_for(system, **options):
    db, clock, jobs, _, _ = system
    cfg = MarketConfig(
        inst_id=INST,
        contract_type="perpetual",
        settlement_asset="USDT",
        trade_minutes=TradeMinuteConfig(
            bootstrap_days=1, max_minutes_per_cycle=2, max_requests_per_cycle=7, **options
        ),
    )
    client = PublicClient(db, clock, cfg)
    return TradeMinuteCollector(
        db, jobs.ownership, client, cfg, inst_ids=(INST, "ETH-USDT-SWAP")
    ), client


async def test_owned_background_tick_is_bounded_and_io_is_outside_transaction(prediction_system):
    db, clock, _, _, _ = prediction_system
    collector, client = collector_for(prediction_system)
    result = await collector.tick()
    assert result == {"requests": 7, "verified": 2, "incomplete": 0}
    assert not any("rubik" in path for path in client.calls)
    async with db.transaction() as conn:
        assert (
            await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_minute_work)) == 2880
        )
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_raw_cache)) == 2
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.market_observations)
                .where(s.market_observations.c.source == MINUTE_SOURCE)
            )
            == 2
        )
    # Persistent work catches new minutes after restart without duplicating completed rows.
    clock.value += timedelta(minutes=2)
    restarted, _ = collector_for(prediction_system)
    assert (await restarted.tick())["verified"] == 2
    async with db.transaction() as conn:
        assert (
            await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_minute_work)) == 2884
        )


async def test_anomaly_retry_repairs_with_append_only_revision(prediction_system):
    db, clock, _, _, _ = prediction_system
    collector, client = collector_for(prediction_system)
    client.mismatch = True
    assert (await collector.tick())["incomplete"] == 2
    async with db.transaction() as conn:
        old = (
            (
                await conn.execute(
                    sa.select(s.market_observations).where(
                        s.market_observations.c.source == MINUTE_SOURCE
                    )
                )
            )
            .mappings()
            .all()
        )
    clock.value += timedelta(seconds=61)
    client.mismatch = False
    # Directly refetch the original minute: no fabricated allocation from candle difference.
    for row in old:
        c = await capture_minute(
            client,
            row["inst_id"],
            row["coverage_start"],
            clock.value + timedelta(seconds=30),
            collector.settings,
            metadata(row["inst_id"]),
        )
        await collector.record(c)
    async with db.transaction() as conn:
        rows = (
            (
                await conn.execute(
                    sa.select(s.market_observations).where(
                        s.market_observations.c.source == MINUTE_SOURCE
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 4
        assert {r["quality"] for r in rows if r["revision_no"] == 1} == {"MISSING"}
        assert {r["quality"] for r in rows if r["revision_no"] == 2} == {"OK"}
        assert {Decimal(r["payload"]["buy"]) for r in rows if r["revision_no"] == 2} == {Decimal(2)}


async def test_expiring_raw_cache_preserves_minute_facts_and_audit_detects_side_change(
    prediction_system,
):
    db, clock, _, _, _ = prediction_system
    collector, client = collector_for(prediction_system)
    await collector.tick()
    async with db.transaction() as conn:
        first = (
            (
                await conn.execute(
                    sa.select(s.market_observations).where(
                        s.market_observations.c.source == MINUTE_SOURCE,
                        s.market_observations.c.inst_id == INST,
                    )
                )
            )
            .mappings()
            .one()
        )
    clock.value += timedelta(minutes=1)
    client.reverse = True
    capture = await capture_minute(
        client,
        INST,
        first["coverage_start"],
        clock.value + timedelta(seconds=30),
        collector.settings,
        metadata(),
    )
    await collector.record(capture, audit=True)
    async with db.transaction() as conn:
        audit = (
            (
                await conn.execute(
                    sa.select(s.market_observations).where(
                        s.market_observations.c.kind == "trade_audit"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert audit["payload"]["allocation_changed"] is True
    clock.value += timedelta(hours=25)
    await collector.plan()
    async with db.transaction() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_raw_cache)) == 0
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.market_observations)
                .where(s.market_observations.c.source == MINUTE_SOURCE)
            )
            == 3
        )
    with pytest.raises(sa.exc.DBAPIError):
        async with db.transaction() as conn:
            await conn.execute(
                sa.delete(s.market_observations).where(s.market_observations.c.id == first["id"])
            )


async def test_stale_owner_cannot_persist_captured_data(prediction_system):
    db, clock, jobs, _, _ = prediction_system
    collector, client = collector_for(prediction_system)
    captured = await capture_minute(
        client,
        INST,
        clock.value.replace(second=0) - timedelta(minutes=2),
        clock.value + timedelta(seconds=30),
        collector.settings,
        metadata(),
    )
    await jobs.ownership.release()
    replacement = RuntimeOwnership(db)
    await replacement.acquire()
    try:
        with pytest.raises(StaleAttempt):
            await collector.record(captured)
        async with db.transaction() as conn:
            assert await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_raw_cache)) == 0
            assert (
                await conn.scalar(sa.select(sa.func.count()).select_from(s.market_observations))
                == 0
            )
    finally:
        await replacement.release()


async def seed_minutes(system, end):
    db, clock, jobs, _, _ = system
    items = []
    for i in range(1200):
        start = end - timedelta(minutes=1200 - i)
        items.append(
            Observation(
                source=MINUTE_SOURCE,
                inst_id=INST,
                kind="taker_volume",
                event_at=start + timedelta(minutes=1),
                available_at=clock.value,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                unit="base_quantity",
                payload={"buy": "2", "sell": "1", "timeframe": "1m", "raw_digest": str(i)},
            )
        )
    async with db.transaction() as conn:
        await jobs.ownership.guard(conn)
        for offset in range(0, len(items), 500):
            await persist_observations(conn, items[offset : offset + 500])
    return items


async def test_formal_freeze_uses_minute_revisions_and_keeps_original_input(prediction_system):
    db, clock, jobs, service, engine = prediction_system
    end = clock.value.replace(minute=0, second=0, microsecond=0)
    items = await seed_minutes(prediction_system, end)
    bars = tuple(
        b.model_copy(update={"volume_base": Decimal(180), "volume_contracts": Decimal(18000)})
        for b in bars_at(end)
    )
    attempt = await task(prediction_system, freeze=False)
    legacy = Observation(
        source="okx_rubik:bad",
        inst_id=INST,
        kind="taker_volume",
        event_at=end,
        available_at=clock.value,
        unit="base_quantity",
        payload={"buy": "999999", "sell": "0"},
    )
    ident = await service.freeze(attempt, engine, bars, (legacy,))
    context = await service.context(attempt)
    feature = next(f for f in context["features"] if f["name"] == "cvd")
    assert feature["quality"] == "OK" and Decimal(str(feature["value"])) == 1200
    assert feature["source"] == "okx_trade_window:1h:v1"
    clock.value += timedelta(seconds=10)
    revised = items[-1].model_copy(
        update={
            "id": uuid4(),
            "available_at": clock.value,
            "payload": {"buy": "1", "sell": "2", "timeframe": "1m"},
        }
    )
    async with db.transaction() as conn:
        await jobs.ownership.guard(conn)
        await persist_observation(conn, revised)
        old = await trade_windows(conn, INST, "1h", end, clock.value - timedelta(seconds=10))
        new = await trade_windows(conn, INST, "1h", end, clock.value)
    assert old[-1].payload["buy"] == 120 and new[-1].payload["buy"] == 119
    assert old[-1].payload["minute_versions_hash"] != new[-1].payload["minute_versions_hash"]
    assert await service.freeze(attempt, engine, bars) == ident
    assert (await service.context(attempt))["features"] == context["features"]


async def test_missing_minutes_do_not_fallback_to_rubik(prediction_system):
    db, clock, _, service, engine = prediction_system
    end = clock.value.replace(minute=0, second=0, microsecond=0)
    attempt = await task(prediction_system, freeze=False)
    legacy = tuple(
        Observation(
            source="okx_rubik:old",
            inst_id=INST,
            kind="taker_volume",
            event_at=end - timedelta(hours=i),
            available_at=clock.value,
            coverage_start=end - timedelta(hours=i + 1),
            coverage_end=end - timedelta(hours=i),
            unit="base_quantity",
            payload={"buy": "2", "sell": "1"},
        )
        for i in range(20)
    )
    await service.freeze(attempt, engine, bars_at(end), legacy)
    context = await service.context(attempt)
    cvd = next(f for f in context["features"] if f["name"] == "cvd")
    assert cvd["value"] is None and cvd["quality"] == "MISSING"
    async with db.transaction() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.market_observations)
                .where(s.market_observations.c.source == "okx_rubik:old")
            )
            == 0
        )


async def test_scheduler_retries_anomaly_after_backoff_without_model_tools(prediction_system):
    db, clock, _, _, _ = prediction_system
    collector, client = collector_for(prediction_system)
    client.mismatch = True
    await collector.tick()
    async with db.transaction() as conn:
        original = (
            (
                await conn.execute(
                    sa.select(s.trade_minute_work).where(s.trade_minute_work.c.state == "ERROR")
                )
            )
            .mappings()
            .all()
        )
    assert len(original) == 2
    collector.settings = collector.settings.model_copy(
        update={"max_minutes_per_cycle": 4, "max_requests_per_cycle": 20}
    )
    clock.value += timedelta(seconds=61)
    client.mismatch = False
    result = await collector.tick()
    assert result["verified"] == 4
    async with db.transaction() as conn:
        for row in original:
            state = await conn.scalar(
                sa.select(s.trade_minute_work.c.state).where(
                    s.trade_minute_work.c.inst_id == row["inst_id"],
                    s.trade_minute_work.c.open_at == row["open_at"],
                )
            )
            assert state == "VERIFIED"


@pytest.mark.parametrize("source", ["trade_minutes", "rubik"])
async def test_runtime_starts_collector_only_after_ownership_and_stops_before_client(
    postgres_url, anchor, tmp_path, monkeypatch, source
):
    import asyncio

    from test_prediction_pipeline import Clock
    from test_structural_runtime import FixtureResources, FixtureSender, PublicMarket

    from trading_agent.api.auth import CredentialHashes
    from trading_agent.config import AppConfig, HermesConfig, PredictionConfig, RuntimeConfig
    from trading_agent.jobs.service import token_hash
    from trading_agent.runtime.lifecycle import Runtime
    from trading_agent.storage.database import Database

    events = []
    clock = Clock(anchor + timedelta(seconds=65))
    db = Database(postgres_url, clock=clock)
    config = AppConfig(
        hermes=HermesConfig(provider="fixture", model="fixture"),
        runtime=RuntimeConfig(data_dir=tmp_path / "runtime"),
        prediction=PredictionConfig(knowledge_index_path=tmp_path / "knowledge.sqlite"),
        market=MarketConfig(taker_source=source),
    )

    class Client(PublicMarket):
        async def close(self):
            events.append("client_closed")

    class SpyCollector:
        def __init__(self, database, ownership, client, cfg, *, inst_ids, on_fatal):
            assert set(inst_ids) == {INST, "ETH-USDT-SWAP"}
            self.ownership = ownership
            events.append("constructed")

        async def start(self):
            assert await self.ownership.probe()
            events.append("started")

        async def stop(self):
            assert await self.ownership.probe()
            assert "client_closed" not in events
            events.append("stopped")

    async def verified():
        return True

    async def idle(*args):
        await asyncio.Event().wait()

    # Keep scheduler/engine startup; model workers are irrelevant to this lifecycle test.
    monkeypatch.setattr(Runtime, "_loop", idle)
    runtime = Runtime(
        config,
        db,
        CredentialHashes(token_hash("controller"), token_hash("operator")),
        FixtureResources(),
        Client(clock),
        verified,
        sender=FixtureSender(),
        trade_collector_factory=SpyCollector,
        fatal_exit=lambda: None,
    )
    try:
        await runtime.start()
        assert await runtime.ownership.probe()
    finally:
        await runtime.stop()
    assert events == (
        ["constructed", "started", "stopped", "client_closed"]
        if source == "trade_minutes"
        else ["client_closed"]
    )
