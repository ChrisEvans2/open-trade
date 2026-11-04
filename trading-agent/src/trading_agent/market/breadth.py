import asyncio
import re
from datetime import timedelta
from decimal import Decimal

from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import INTERVAL_SECONDS, OKX_BARS, TIMEFRAMES, boundary
from trading_agent.market.models import Observation
from trading_agent.market.observations import number, timestamp


def member_history(inst_id, timeframe, rows, available_at, end):
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+(?:-(?:SWAP|[0-9]{6}))?", inst_id):
        raise ValueError("basket requires an explicit venue instrument ID")
    seconds = INTERVAL_SECONDS[timeframe]
    indexed = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
            raise ValueError("invalid basket candle")
        if row[8] == "0":
            continue
        at = timestamp(row[0])
        closed = at + timedelta(seconds=seconds)
        if closed > end:
            continue
        if at.timestamp() % seconds or closed > available_at or at in indexed:
            raise ValueError("invalid basket candle time")
        opened, high, low, close = (number(v, nonnegative=True) for v in row[1:5])
        if low <= 0 or not low <= opened <= high or not low <= close <= high:
            raise ValueError("invalid basket price")
        for amount in row[5:8]:
            number(amount, nonnegative=True)
        indexed[at] = (row, close)
    expected = [end - timedelta(seconds=seconds * i) for i in range(20, 0, -1)]
    if any(at not in indexed for at in expected):
        raise ValueError("basket history must cover 20 contiguous closed bars")
    selected = [indexed[at] for at in expected]
    above = selected[-1][1] > sum((close for _, close in selected), Decimal(0)) / 20
    return {
        "inst_id": inst_id,
        "market_type": "derivatives" if inst_id.count("-") == 2 else "spot",
        "quality": "OK",
        "above_sma20": above,
        "raw_bars": [row for row, _ in selected],
    }


async def collect_breadth(client, deadline, *, timeframes=TIMEFRAMES, anchor=None):
    features = getattr(client, "feature_config", None)
    basket = features.market_basket if features else ()
    if not basket:
        return ()
    anchor = anchor or boundary(client.now())

    async def timeframe(tf):
        end = boundary(anchor, tf)

        async def member(inst_id):
            try:
                rows = await client.get(
                    "/api/v5/market/history-candles",
                    {
                        "instId": inst_id,
                        "bar": OKX_BARS[tf],
                        "after": str(int(end.timestamp() * 1000)),
                        "limit": "30",
                    },
                    deadline,
                )
                return member_history(inst_id, tf, rows, client.now(), end)
            except (DomainError, ValueError, TypeError, KeyError):
                return {
                    "inst_id": inst_id,
                    "quality": "MISSING",
                    "reason": "BASKET_MEMBER_UNAVAILABLE_OR_INVALID",
                }

        members = await asyncio.gather(*(member(inst_id) for inst_id in basket))
        covered = [m for m in members if m["quality"] == "OK"]
        at = client.now()
        return Observation(
            source=f"okx_breadth:{tf}:v1",
            inst_id=client.config.inst_id,
            kind="market_breadth",
            event_at=end if covered else at,
            available_at=at,
            unit="fraction",
            quality="OK" if covered else "MISSING",
            payload={
                "timeframe": tf,
                "basket": list(basket),
                "covered": len(covered),
                "above_sma20": sum(m["above_sma20"] for m in covered),
                "members": members,
                "reason": None if covered else "NO_VALID_BASKET_MEMBERS",
            },
        )

    return tuple(await asyncio.gather(*(timeframe(tf) for tf in timeframes)))
