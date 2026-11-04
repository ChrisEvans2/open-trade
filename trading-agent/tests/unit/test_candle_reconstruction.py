from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import bars_at

from trading_agent.config import FeatureConfig, MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import boundary
from trading_agent.market.models import Bar
from trading_agent.market.okx import OKXClient
from trading_agent.market.reconstruction import reconstruct_candle, reconstruct_input

TARGET = datetime(2026, 10, 30, 7, 23, tzinfo=UTC)  # Beijing 15:23
COLLECTED = TARGET + timedelta(days=1)


def minutes(timeframe="4h", *, inst_id="BTC-USDT-SWAP", target=TARGET):
    start, end = boundary(target, timeframe), boundary(target, "1m")
    return tuple(
        Bar(
            inst_id=inst_id,
            timeframe="1m",
            open_at=start + timedelta(minutes=i),
            close_boundary=start + timedelta(minutes=i + 1),
            open=100 + i,
            high=102 + i,
            low=99 + i,
            close=101 + i,
            volume_contracts=1,
            volume_base=".1",
            turnover_quote=15,
            available_at=COLLECTED,
        )
        for i in range(int((end - start).total_seconds() // 60))
    )


def rebuild(rows, timeframe="4h", target=TARGET, inst_id="BTC-USDT-SWAP"):
    return reconstruct_candle(
        rows, inst_id=inst_id, timeframe=timeframe, target_at=target, collected_by=COLLECTED
    )


def test_1523_ohlcv_is_only_the_203_completed_minutes():
    rows = minutes()
    candle = rebuild(rows[::-1])
    assert candle.open_at == TARGET.replace(hour=4, minute=0)
    assert candle.close_boundary == TARGET.replace(hour=8, minute=0)
    assert (candle.open, candle.high, candle.low, candle.close) == (100, 304, 99, 303)
    assert (candle.volume_contracts, candle.volume_base, candle.turnover_quote) == (
        203,
        Decimal("20.3"),
        3045,
    )
    assert candle.data_cutoff == TARGET and candle.collected_at == COLLECTED
    assert candle.status == "RECONSTRUCTED"
    # No value from the unfinished 15:23–15:24 minute, or the final 16:00 bar, is admitted.
    future = rows[-1].model_copy(
        update={
            "open_at": TARGET,
            "close_boundary": TARGET + timedelta(minutes=1),
            "high": Decimal(999),
            "close": Decimal(999),
        }
    )
    with pytest.raises(DomainError, match="截止时间之后"):
        rebuild((*rows, future))


@pytest.mark.parametrize("inst_id", ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
@pytest.mark.parametrize("timeframe", ["1h", "4h", "6h", "12h"])
def test_all_scopes_share_exact_formulas_and_keep_reconstruction_clock(inst_id, timeframe):
    target = TARGET + timedelta(seconds=45)
    rows = minutes(timeframe, target=target, inst_id=inst_id)
    closed = tuple(
        b.model_copy(update={"inst_id": inst_id}) for b in bars_at(boundary(target), timeframe)
    )
    original = {
        "body": {"inst_id": inst_id, "bar_ids": [str(b.id) for b in closed]},
        "timeframe": timeframe,
        "decision_at": target,
    }
    value = reconstruct_input(original, closed, rows, FeatureConfig(), reconstructed_at=COLLECTED)
    assert value["quality"] == "PARTIAL_COVERAGE"
    assert value["missing_reason"] == "SUBMINUTE_NOT_AVAILABLE" and value["uncovered_seconds"] == 45
    assert value["forming_candle"]["data_cutoff"] == json_value(TARGET)
    assert not value["originally_observed"] and not value["availability_at_target_verified"]
    assert value["minute_source_hash"] == content_hash(value["minute_source"])
    features = {f["name"]: f for f in value["provisional_features"]}
    assert len(features) == 26
    assert features["return_1"]["value"] == pytest.approx(float(rows[-1].close) / 179 - 1)
    assert all(f["status"] == "PROVISIONAL" for f in features.values())
    assert all(f["source"].startswith("reconstructed:provisional:") for f in features.values())
    assert all(f["available_at"] == json_value(COLLECTED) for f in features.values())
    assert all(f["event_at"] == json_value(TARGET) for f in features.values())
    assert features["rvol"]["quality"] == "PARTIAL_COVERAGE"
    assert features["volume_zscore"]["quality"] == "PARTIAL_COVERAGE"
    with pytest.raises(DomainError, match="原始计算窗口"):
        reconstruct_input(original, closed[1:], rows, FeatureConfig(), reconstructed_at=COLLECTED)


@pytest.mark.parametrize(
    "fault,code",
    [
        ("gap", "MINUTE_COVERAGE_GAP"),
        ("duplicate", "MINUTE_COVERAGE_GAP"),
        ("empty", "MINUTE_COVERAGE_GAP"),
        ("symbol", "RECONSTRUCTION_SCOPE"),
        ("future_receipt", "RECONSTRUCTION_SCOPE"),
        ("old", "RECONSTRUCTION_SCOPE"),
    ],
)
def test_no_invented_data_on_bad_minutes(fault, code):
    rows = minutes()
    if fault == "gap":
        rows = rows[:100] + rows[101:]
    elif fault == "duplicate":
        rows = rows[:100] + (rows[99],) + rows[101:]
    elif fault == "empty":
        rows = ()
    elif fault == "symbol":
        rows = (rows[0].model_copy(update={"inst_id": "ETH-USDT-SWAP"}),) + rows[1:]
    elif fault == "future_receipt":
        rows = (
            rows[0].model_copy(update={"available_at": COLLECTED + timedelta(seconds=1)}),
        ) + rows[1:]
    else:
        rows = (
            rows[0].model_copy(
                update={
                    "open_at": rows[0].open_at - timedelta(minutes=1),
                    "close_boundary": rows[0].open_at,
                }
            ),
        ) + rows[1:]
    with pytest.raises(DomainError) as caught:
        rebuild(rows)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "target", [TARGET.replace(hour=8, minute=0), TARGET.replace(hour=8, minute=0, second=45)]
)
def test_new_period_has_no_completed_minute(target):
    with pytest.raises(DomainError) as caught:
        rebuild((), target=target)
    assert caught.value.code == "NO_COMPLETED_MINUTE"


async def test_actual_minute_client_paginates_12h_and_filters_target_minute():
    target = TARGET.replace(hour=11, minute=59, second=45)
    seen = []

    def response(request):
        seen.append(request)
        assert request.url.path == "/api/v5/market/history-candles"
        assert request.url.params["bar"] == "1m"
        end = int(request.url.params["after"]) // 1000
        # Include an extra boundary row to check actual client-side cutoff filtering.
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    [str((end - i * 60) * 1000), "100", "102", "99", "101", "1", ".1", "15", "1"]
                    for i in range(301)
                ],
            },
        )

    client = OKXClient(
        MarketConfig(), now=lambda: COLLECTED, transport=httpx.MockTransport(response)
    )
    try:
        rows = await client.minute_window(
            "ETH-USDT-SWAP",
            boundary(target, "12h"),
            boundary(target, "1m"),
            COLLECTED + timedelta(seconds=40),
        )
        value = rebuild(rows, timeframe="12h", target=target, inst_id="ETH-USDT-SWAP")
        assert len(seen) == 3 and len(rows) == 719
        assert value.volume_contracts == 719 and value.data_cutoff == target.replace(second=0)
    finally:
        await client.close()
