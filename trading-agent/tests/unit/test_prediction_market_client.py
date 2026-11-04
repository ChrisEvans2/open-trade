from datetime import timedelta

import httpx

from trading_agent.config import MarketConfig
from trading_agent.market.okx import OKXClient


async def test_minutes_page_oldest_first_and_stop_at_gap(anchor):
    seen, missing = [], {anchor + timedelta(minutes=350)}

    async def response(request):
        assert request.url.params["instId"] == "ETH-USDT-SWAP"
        end = int(request.url.params["after"]) // 1000
        seen.append(end)
        rows = []
        for i in range(300):
            at = anchor.fromtimestamp(end - (i + 1) * 60, anchor.tzinfo)
            if at in missing:
                continue
            rows.append(
                [str(int(at.timestamp() * 1000)), "100", "102", "98", "101", "10", "1", "100", "1"]
            )
        return httpx.Response(200, json={"code": "0", "data": rows})

    now = anchor + timedelta(minutes=900)
    client = OKXClient(MarketConfig(), transport=httpx.MockTransport(response), now=lambda: now)
    try:
        rows = await client.minute_window("ETH-USDT-SWAP", anchor, now, now + timedelta(seconds=30))
        assert len(seen) == 2 and len(rows) == 599
        assert rows[0].open_at == anchor and rows[-1].close_boundary == anchor + timedelta(
            minutes=600
        )
        assert all(b.inst_id == "ETH-USDT-SWAP" for b in rows)
        missing.clear()
        next_rows = await client.minute_window(
            "ETH-USDT-SWAP", anchor + timedelta(minutes=350), now, now + timedelta(seconds=30)
        )
        assert len(next_rows) == 550 and next_rows[0].open_at == anchor + timedelta(minutes=350)
    finally:
        await client.close()
