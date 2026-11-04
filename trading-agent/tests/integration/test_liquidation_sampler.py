import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import bars_at
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from trading_agent.config import FeatureConfig, MarketConfig
from trading_agent.domain.errors import StaleAttempt
from trading_agent.market.features import compute_features
from trading_agent.market.liquidations import RAW_PREFIX, STREAM_SOURCE, LiquidationSampler
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database

pytestmark = pytest.mark.postgres


def configuration():
    return MarketConfig(inst_id="BTC-USDT-SWAP", contract_type="perpetual", settlement_asset="USDT")


def instrument_metadata():
    return {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "instFamily": "BTC-USDT",
        "settleCcy": "USDT",
        "state": "live",
        "ctType": "linear",
        "ctVal": "0.01",
        "ctMult": "1",
        "ctValCcy": "BTC",
    }


def frame(at, side="sell", quantity="3"):
    return {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "details": [
                    {
                        "side": side,
                        "posSide": "long" if side == "sell" else "short",
                        "sz": quantity,
                        "bkPx": "60000",
                        "bkLoss": "0",
                        "ccy": "",
                        "ts": str(int(at.timestamp() * 1000)),
                    }
                ],
            }
        ],
    }


async def test_durable_dedup_gaps_late_events_and_takeover(postgres_url, anchor, monkeypatch):
    clock = [anchor - timedelta(hours=1)]
    database = Database(postgres_url, clock=lambda: clock[0])
    owner = RuntimeOwnership(database)
    replacement = RuntimeOwnership(database)
    await owner.acquire()
    sampler = LiquidationSampler(database, owner, configuration())
    try:
        await sampler._marker("OPEN")
        clock[0] = anchor - timedelta(minutes=30)
        await sampler._marker("COVERAGE")
        clock[0] = anchor - timedelta(minutes=20)
        await sampler._gap("DISCONNECTED")
        await sampler._marker("OPEN")
        clock[0] = anchor + timedelta(seconds=20)
        await sampler._marker("COVERAGE")
        clock[0] = anchor + timedelta(seconds=60)
        message = frame(anchor - timedelta(minutes=10))
        message["data"][0]["details"] += frame(anchor - timedelta(minutes=11), "buy", "7")["data"][
            0
        ]["details"]
        message["data"][0]["details"][1]["bkPx"] = "30000"
        await sampler.record_frame(message)
        await sampler.record_frame(message)
        async with database.transaction() as connection:
            assert (
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(s.market_observations)
                    .where(
                        s.market_observations.c.source.startswith(RAW_PREFIX, autoescape=True),
                    )
                )
                == 2
            )
            original = await sampler.windows(
                connection, anchor, clock[0], instrument=instrument_metadata()
            )
        feature = {
            f.name: f
            for f in compute_features(bars_at(anchor), FeatureConfig(), original, clock[0])
        }["liquidation_imbalance"]
        assert feature.value == pytest.approx(
            1 / 13
        )  # Amount weighting differs from contract counts.
        assert feature.quality == "PARTIAL_COVERAGE" and "1处缺口" in feature.missing_reason
        assert original[0].payload["gaps"] == [
            {
                "start": (anchor - timedelta(minutes=30)).isoformat(),
                "end": (anchor - timedelta(minutes=20)).isoformat(),
            }
        ]
        assert original[0].payload["long_quote"] == 1800
        assert original[0].payload["short_quote"] == 2100
        assert original[0].unit == "quote_USDT"
        clock[0] = anchor + timedelta(seconds=80)
        await sampler._marker("COVERAGE")
        async with database.transaction() as connection:
            repeated = await sampler.windows(
                connection, anchor, clock[0], instrument=instrument_metadata()
            )
        assert repeated[0].id == original[0].id  # A later heartbeat cannot rewrite a closed window.
        await sampler.record_frame(frame(anchor - timedelta(minutes=12), "buy", "5"))
        async with database.transaction() as connection:
            corrected = await sampler.windows(
                connection, anchor, clock[0], instrument=instrument_metadata()
            )
        assert corrected[0].revision_no == 2
        assert original[0].payload["short_contracts"] == 7
        assert corrected[0].payload["short_contracts"] == 12

        monkeypatch.setattr("trading_agent.market.liquidations.MAX_EVENTS", 1)
        async with database.transaction() as connection:
            limited = await sampler.windows(
                connection, anchor, clock[0], instrument=instrument_metadata()
            )
        assert limited[0].quality == "MISSING" and limited[0].payload["truncated"]
        missing = {
            f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), limited, clock[0])
        }["liquidation_imbalance"]
        assert missing.value is None and missing.missing_reason == "SAMPLE_QUERY_LIMIT"

        await owner.release()
        await replacement.acquire()
        with pytest.raises(StaleAttempt):
            await sampler.record_frame(frame(anchor - timedelta(minutes=5)))
    finally:
        await replacement.release()
        await owner.release()
        await database.close()


@pytest.mark.parametrize("port_notice", [False, True])
async def test_real_loopback_websocket_records_heartbeat_disconnect_and_restart(
    database, port_notice
):
    owner = RuntimeOwnership(database)
    await owner.acquire()
    completed = asyncio.Event()

    async def server(socket):
        request = json.loads(await socket.recv())
        assert request == {
            "id": "liquidations",
            "op": "subscribe",
            "args": [
                {"channel": "liquidation-orders", "instType": "SWAP"},
            ],
        }
        if port_notice:
            await socket.send(json.dumps({"event": "notice", "code": "64010"}))
        await socket.send(
            json.dumps({"id": request["id"], "event": "subscribe", "arg": request["args"][0]})
        )
        await socket.send(json.dumps(frame(datetime.now(UTC) - timedelta(seconds=2))))
        assert await socket.recv() == "ping"
        if port_notice:
            await socket.send(json.dumps({"event": "notice", "code": "64010"}))
        await socket.send("pong")
        assert await socket.recv() == "ping"
        await socket.close()
        completed.set()

    sampler = None
    try:
        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]

            def connector(url, **kwargs):
                assert url == configuration().public_ws_url
                assert database.engine.pool.checkedout() == 1  # Only the dedicated instance lock.
                return connect(f"ws://127.0.0.1:{port}", proxy=None, **kwargs)

            sampler = LiquidationSampler(database, owner, configuration(), connector=connector)
            sampler.heartbeat_seconds, sampler.pong_seconds = 0.03, 0.2
            await sampler.start()
            await asyncio.wait_for(completed.wait(), 3)
            async with asyncio.timeout(3):
                for _ in range(300):
                    if sampler.failure_code is not None:
                        break
                    await asyncio.sleep(0.01)
            assert sampler.failure_code == "LIQUIDATION_SAMPLE_DISCONNECTED"
            await sampler.stop()
        async with database.engine.connect() as connection:
            records = (await connection.execute(sa.select(s.market_observations))).mappings().all()
        assert sum(r["source"].startswith(RAW_PREFIX) for r in records) == 1
        assert any(r["payload"].get("record") == "COVERAGE" for r in records)
        assert any(r["payload"].get("record") == "GAP" for r in records)
        assert not sampler.connected
        restart = LiquidationSampler(database, owner, configuration(), connector=connector)
        await restart.start()
        await restart.stop()
        async with database.engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(s.market_observations)
                    .where(
                        s.market_observations.c.source == STREAM_SOURCE,
                        s.market_observations.c.payload["reason"].astext == "RUNTIME_RESTART",
                    )
                )
                == 1
            )
    finally:
        if sampler:
            await sampler.stop()
        await owner.release()
