"""Independent boundary examples for minute trade reconstruction."""

from datetime import timedelta

import pytest

from trading_agent.config import TradeMinuteConfig
from trading_agent.domain.errors import DomainError
from trading_agent.market.okx import OKXClient
from trading_agent.market.trades import capture_minute


class PartialSource:
    parse_bar = OKXClient.parse_bar

    def __init__(self, start, *, unknown_old_source=False):
        self.start = start
        self.unknown_old_source = unknown_old_source
        self.pages = 0

    def now(self):
        return self.start + timedelta(minutes=2)

    async def get(self, path, params, deadline):
        if path.endswith("history-candles"):
            return [
                [
                    str(int(self.start.timestamp() * 1000)),
                    "100",
                    "100",
                    "100",
                    "100",
                    "500",
                    "5",
                    "500",
                    "1",
                ]
            ]
        self.pages += 1
        if self.pages > 1:
            raise DomainError("TRADE_CYCLE_BUDGET", "fixture exhausted budget")
        rows = [
            dict(
                instId="BTC-USDT-SWAP",
                tradeId=str(ident),
                ts=str(int((self.start + timedelta(seconds=second)).timestamp() * 1000)),
                sz=size,
                px="100",
                side=side,
                source=source,
            )
            for ident, second, size, side, source in (
                (30, 50, "300", "sell", "1"),
                (20, 10, "200", "buy", "0"),
            )
        ]
        if self.unknown_old_source:
            rows.append(
                {
                    **rows[-1],
                    "tradeId": "10",
                    "source": "unexpected",
                    "ts": str(int((self.start - timedelta(seconds=1)).timestamp() * 1000)),
                }
            )
        return rows


def contract():
    return dict(
        instId="BTC-USDT-SWAP",
        instType="SWAP",
        ctType="linear",
        settleCcy="USDT",
        ctValCcy="BTC",
        ctVal="0.01",
        ctMult="1",
    )


async def test_matching_total_does_not_replace_proof_of_pagination_coverage(anchor):
    client = PartialSource(anchor)
    result = await capture_minute(
        client,
        "BTC-USDT-SWAP",
        anchor,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(),
        contract(),
    )
    assert result.observation.quality == "MISSING"
    assert result.observation.payload["reason"] == "TRADE_CYCLE_BUDGET"
    assert result.observation.payload["pagination_complete"] is False
    assert len(result.rows) == 2  # Retained for diagnosis, not declared a complete minute.
    assert result.candle.volume_base == 5


async def test_unrecognized_source_at_older_boundary_cannot_certify_complete_minute(anchor):
    client = PartialSource(anchor, unknown_old_source=True)
    result = await capture_minute(
        client,
        "BTC-USDT-SWAP",
        anchor,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(),
        contract(),
    )
    assert result.observation.quality == "MISSING"
    assert result.observation.payload["reason"] == "TRADE_CLASSIFICATION"
    assert result.observation.payload["pagination_complete"] is False


@pytest.mark.parametrize(
    "field,value", [("ctMult", "2"), ("ctValCcy", "USDT"), ("ctType", "inverse")]
)
async def test_unsupported_contract_units_are_rejected_before_public_io(anchor, field, value):
    client = PartialSource(anchor)
    result = await capture_minute(
        client,
        "BTC-USDT-SWAP",
        anchor,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(),
        {**contract(), field: value},
    )
    assert result.observation.quality == "MISSING"
    assert result.observation.payload["reason"] == "TRADE_CONTRACT_UNSUPPORTED"
    assert result.rows == () and client.pages == 0 and result.candle is None
