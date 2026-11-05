"""Independent review cases for page boundaries and resumed calculations."""

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_agent.config import TradeMinuteConfig
from trading_agent.domain.errors import StaleAttempt
from trading_agent.market.okx import OKXClient
from trading_agent.market.trades import capture_minute

INST = "BTC-USDT-SWAP"
META = dict(
    instId=INST,
    instType="SWAP",
    ctType="linear",
    settleCcy="USDT",
    ctValCcy="BTC",
    ctVal="0.01",
    ctMult="1",
)


def row(start, ident, seconds, side="buy", size="100"):
    return dict(
        instId=INST,
        tradeId=str(ident),
        ts=str(int((start + timedelta(seconds=seconds)).timestamp() * 1000)),
        side=side,
        sz=size,
        px="100",
        source="0",
    )


class ReviewClient:
    parse_bar = OKXClient.parse_bar

    def __init__(self, start, pages):
        self.start, self.pages = start, list(pages)
        self.calls = []

    def now(self):
        return self.start + timedelta(minutes=5)

    async def get(self, path, params, deadline):
        self.calls.append((path, dict(params)))
        if path.endswith("history-candles"):
            return [[str(int(self.start.timestamp() * 1000)), *["100"] * 4, "600", "6", "600", "1"]]
        return self.pages.pop(0)


async def run(client, **options):
    return await capture_minute(
        client,
        INST,
        client.start,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(),
        META,
        **options,
    )


async def first_checkpoint(start):
    states = []
    first = [row(start, 4, 50, "sell", "300"), row(start, 3, 30, "buy", "200")]

    async def save(state, rows):
        states.append({**state, "rows": list(rows)})

    capture = await run(ReviewClient(start, [first]), page_limit=1, on_page=save)
    assert capture.observation.payload["reason"] == "TRADE_PAGE_SLICE"
    return states[0]


async def test_restart_uses_saved_cursor_and_deduplicates_overlap(anchor):
    state = await first_checkpoint(anchor)
    client = ReviewClient(
        anchor,
        [
            [
                state["rows"][-1],
                row(anchor, 2, 0),
                row(anchor, 1, -1),
            ]
        ],
    )
    result = await run(client, resume=state, page_limit=1)
    assert client.calls[0][1] == {
        "instId": INST,
        "type": "1",
        "after": "3",
        "limit": "100",
    }
    assert len(client.calls) == 1
    assert result.observation.quality == "OK"
    assert result.observation.payload["buy"] == Decimal(3)
    assert result.observation.payload["sell"] == Decimal(3)
    assert result.observation.payload["trade_count"] == 3


async def test_conflicting_resumed_page_does_not_save_a_new_cursor(anchor):
    state = await first_checkpoint(anchor)
    saves = []

    async def save(next_state, rows):
        saves.append(next_state)

    duplicate = {**state["rows"][-1], "side": "sell"}
    result = await run(
        ReviewClient(anchor, [[duplicate, row(anchor, 2, 0), row(anchor, 1, -1)]]),
        resume=state,
        page_limit=1,
        on_page=save,
    )
    assert result.observation.payload["reason"] == "TRADE_DUPLICATE_CONFLICT"
    assert saves == []
    assert state["cursor"] == 3 and state["pages"] == 1


async def test_lost_fence_during_page_commit_stops_capture(anchor):
    async def save(state, rows):
        raise StaleAttempt()

    with pytest.raises(StaleAttempt):
        await run(ReviewClient(anchor, [[row(anchor, 2, 0)]]), on_page=save, page_limit=1)


async def test_restart_after_final_page_commit_needs_no_network(anchor):
    states = []

    async def save(state, rows):
        states.append({**state, "rows": list(rows)})

    rows = [
        row(anchor, 4, 50, "sell", "300"),
        row(anchor, 3, 30, "buy", "200"),
        row(anchor, 2, 0),
        row(anchor, 1, -1),
    ]
    assert (await run(ReviewClient(anchor, [rows]), on_page=save)).observation.quality == "OK"
    restarted = ReviewClient(anchor, [])
    result = await run(restarted, resume=states[0])
    assert result.observation.quality == "OK"
    assert restarted.calls == []
    assert result.observation.payload["trade_count"] == 3
