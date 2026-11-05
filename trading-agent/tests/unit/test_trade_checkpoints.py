from datetime import timedelta

import httpx
import pytest
from test_trade_minutes import INST, Client, metadata, trade

from trading_agent.config import MarketConfig, TradeMinuteConfig
from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.market.okx import OKXClient
from trading_agent.market.trades import capture_minute


async def test_page_resume_replays_duplicate_without_double_counting(anchor):
    client = Client(anchor, volume="5")
    first = trade(anchor + timedelta(seconds=40), 3, "sell", "300")
    client.pages = [[first], [first, trade(anchor, 2), trade(anchor - timedelta(seconds=1), 1)]]
    saved, rows = {}, []

    async def checkpoint(state, page):
        saved.update(state)
        rows.extend(page)

    args = (
        client,
        INST,
        anchor,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(),
        metadata(),
    )
    partial = await capture_minute(*args, page_limit=1, on_page=checkpoint)
    assert partial.observation.payload["reason"] == "TRADE_PAGE_SLICE"
    assert saved["cursor"] == 3
    resumed = await capture_minute(
        *args, resume={**saved, "rows": rows}, on_page=checkpoint, page_limit=1
    )
    assert resumed.observation.quality == "OK"
    assert resumed.observation.payload["buy"] == 2
    assert resumed.observation.payload["sell"] == 3
    assert len(resumed.rows) == 2
    assert client.calls[-1][1]["after"] == "3"
    assert sum(path.endswith("history-candles") for path, _ in client.calls) == 1


async def test_lost_ownership_during_page_commit_is_not_swallowed(anchor):
    client = Client(anchor)

    async def rejected(state, page):
        raise StaleAttempt()

    with pytest.raises(StaleAttempt):
        await capture_minute(
            client,
            INST,
            anchor,
            client.now() + timedelta(seconds=30),
            TradeMinuteConfig(),
            metadata(),
            on_page=rejected,
        )


async def test_duplicate_conflict_inside_page_does_not_commit_cursor(anchor):
    row = trade(anchor, 3)
    client = Client(anchor, [[row, {**row, "sz": "5"}]])
    commits = []

    async def checkpoint(state, page):
        commits.append(state)

    result = await capture_minute(
        client,
        INST,
        anchor,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(),
        metadata(),
        on_page=checkpoint,
    )
    assert result.observation.payload["reason"] == "TRADE_DUPLICATE_CONFLICT"
    assert not commits


@pytest.mark.parametrize("http_status,code", [(200, "50011"), (429, "0")])
async def test_public_rate_limits_are_distinguished_from_source_rejection(
    anchor, http_status, code, monkeypatch
):
    cfg = MarketConfig()
    client = OKXClient(
        cfg,
        now=lambda: anchor,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                http_status, json={"code": code, "data": []}, headers={"Retry-After": "1"}
            )
        ),
    )

    async def no_wait(*args):
        pass

    monkeypatch.setattr(client, "_wait", no_wait)
    monkeypatch.setattr(client, "_gate", no_wait)
    try:
        with pytest.raises(DomainError) as error:
            await client.get("/api/v5/market/history-trades", {}, anchor + timedelta(seconds=10))
        assert error.value.code == "MARKET_RATE_LIMIT"
        assert client._blocked_until > 0
    finally:
        await client.close()
