from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_agent.config import MarketConfig
from trading_agent.market.liquidations import contract_metadata, parse_frame, uncovered


def frame(at, *, side="sell", position="long", quantity="3"):
    return {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "details": [
                    {
                        "side": side,
                        "posSide": position,
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


def config():
    return MarketConfig(inst_id="BTC-USDT-SWAP", contract_type="perpetual", settlement_asset="USDT")


def test_public_events_preserve_contract_units_and_dedupe_out_of_order(anchor):
    message = frame(anchor - timedelta(minutes=1))
    earlier = frame(anchor - timedelta(minutes=2), side="buy", position="net", quantity="7")
    message["data"][0]["details"] += earlier["data"][0]["details"] * 2
    message["data"].append({"instId": "ETH-USDT-SWAP", "details": "ignored-other-instrument"})
    events = parse_frame(message, config(), anchor)
    assert len(events) == 2
    assert events[0].unit == "contracts"
    assert events[0].payload["contracts"] == Decimal(3)
    assert events[1].payload["liquidated_side"] == "short"
    assert events[1].event_at < events[0].event_at
    assert (
        parse_frame(message, config(), anchor + timedelta(seconds=60))[0].source == events[0].source
    )


@pytest.mark.parametrize(
    "change", ["future", "nan", "direction", "private", "negative", "empty-price"]
)
def test_invalid_event_does_not_become_a_usable_sample(anchor, change):
    message = deepcopy(frame(anchor))
    detail = message["data"][0]["details"][0]
    if change == "future":
        detail["ts"] = str(int((anchor + timedelta(seconds=1)).timestamp() * 1000))
    elif change == "nan":
        detail["sz"] = "NaN"
    elif change == "direction":
        detail["posSide"] = "short"
    elif change == "private":
        message["arg"]["channel"] = "orders"
    elif change == "negative":
        detail["sz"] = "-1"
    else:
        detail["bkPx"] = ""
    with pytest.raises(ValueError):
        parse_frame(message, config(), anchor)


def test_connection_segments_cannot_cover_disconnects(anchor):
    start = anchor - timedelta(hours=1)
    minute = timedelta(minutes=1)
    assert uncovered(start, anchor, [(start, start + minute), (start + 2 * minute, anchor)]) == [
        (start + minute, start + 2 * minute)
    ]
    assert uncovered(start, anchor, []) == [(start, anchor)]
    assert uncovered(start, anchor, [(start - minute, anchor + minute)]) == []


@pytest.mark.parametrize(
    "url",
    [
        "wss://ws.okx.com:8443/ws/v5/private",
        "ws://ws.okx.com/ws/v5/public",
        "wss://user:key@ws.okx.com/ws/v5/public",
        "wss://ws.okx.com/ws/v5/public?key=x",
    ],
)
def test_sampler_configuration_cannot_use_private_or_authenticated_urls(url):
    with pytest.raises(ValidationError):
        MarketConfig(public_ws_url=url)


def test_quote_amount_requires_explicit_contract_face_currency_and_multiplier():
    metadata = {
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
    assert contract_metadata(metadata, config())["face_value"] == Decimal("0.01")
    for replacement in (
        {"ctMult": ""},
        {"ctMult": "2"},
        {"ctValCcy": "USDT"},
        {"ctType": "inverse"},
    ):
        with pytest.raises(ValueError):
            contract_metadata({**metadata, **replacement}, config())
    inverse_config = MarketConfig(
        inst_id="BTC-USD-SWAP", contract_type="perpetual", settlement_asset="BTC"
    )
    inverse = {
        **metadata,
        "instId": "BTC-USD-SWAP",
        "instFamily": "BTC-USD",
        "settleCcy": "BTC",
        "ctType": "inverse",
        "ctVal": "100",
        "ctValCcy": "USD",
    }
    assert contract_metadata(inverse, inverse_config)["quote_currency"] == "USD"
