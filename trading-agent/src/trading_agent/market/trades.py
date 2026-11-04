"""Bounded, public trade reconstruction of one completed minute (no database I/O)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.market.models import Observation
from trading_agent.market.observations import number, timestamp

MINUTE_SOURCE = "okx_trade_minutes:v1"
TRADE_PATH = "/api/v5/market/history-trades"


def fail(code):
    raise DomainError(code, "逐笔成交校验未通过", 503)


def face_value(metadata, inst):
    if (
        inst not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
        or metadata.get("instId") != inst
        or metadata.get("instType") != "SWAP"
        or metadata.get("ctType") != "linear"
        or metadata.get("settleCcy") != "USDT"
        or metadata.get("ctValCcy") != inst.split("-")[0]
        or number(metadata.get("ctMult")) != 1
    ):
        fail("TRADE_CONTRACT_UNSUPPORTED")
    face = number(metadata.get("ctVal"))
    if face <= 0:
        fail("TRADE_CONTRACT_UNSUPPORTED")
    return face


def validate_trade(row, inst, available_at):
    if not isinstance(row, dict) or row.get("instId") != inst:
        fail("TRADE_SCOPE")
    if row.get("side") not in {"buy", "sell"} or row.get("source") not in {"0", "1"}:
        fail("TRADE_CLASSIFICATION")
    ident = str(row.get("tradeId", ""))
    if not ident.isascii() or not ident.isdigit() or int(ident) <= 0:
        fail("TRADE_ID_INVALID")
    at = timestamp(row["ts"])
    if at > available_at or number(row["sz"]) <= 0 or number(row["px"]) <= 0:
        fail("TRADE_FIELDS_INVALID")
    return ident, at


def aggregate(rows, face):
    sides = {"buy": Decimal(0), "sell": Decimal(0)}
    counts = {"buy": 0, "sell": 0}
    sources = defaultdict(lambda: {"buy": Decimal(0), "sell": Decimal(0)})
    for row in rows:
        side, volume = row["side"], number(row["sz"]) * face
        sides[side] += volume
        counts[side] += 1
        sources[row["source"]][side] += volume
    return {**sides, "side_counts": counts, "by_source": dict(sources)}


def verify_direction(rows, face, totals):
    # Separate filtering path: a wrong mapping in aggregate cannot pass just by
    # keeping buy+sell unchanged. This still trusts the exchange's raw side field.
    buys = sum((number(r["sz"]) for r in rows if r["side"] == "buy"), Decimal(0)) * face
    sells = sum((number(r["sz"]) for r in rows if r["side"] == "sell"), Decimal(0)) * face
    return totals["buy"] == buys and totals["sell"] == sells


def same_volume(left, right):
    return abs(left - right) <= max(Decimal("1e-8"), abs(right) * Decimal("1e-6"))


@dataclass
class MinuteCapture:
    observation: Observation
    rows: tuple
    candle: object | None


async def capture_minute(client, inst, start, deadline, settings, metadata, get=None):
    get = get or client.get
    end = start + timedelta(minutes=1)
    selected, candle, totals, pages = {}, None, {}, 0
    complete, reason = False, None
    try:
        if start.second or start.microsecond or end > client.now():
            fail("TRADE_MINUTE_UNCLOSED")
        face = face_value(metadata, inst)
        candles = await get(
            "/api/v5/market/history-candles",
            {"instId": inst, "bar": "1m", "after": str(int(end.timestamp() * 1000)), "limit": "2"},
            deadline,
        )
        matches = [
            b
            for row in candles
            if (b := client.parse_bar(row, "1m", client.now(), inst_id=inst)) is not None
            and b.open_at == start
        ]
        if len(matches) != 1:
            fail("TRADE_CANDLE_UNAVAILABLE")
        candle = matches[0]
        seen = {}
        params = {
            "instId": inst,
            "type": "2",
            "after": str(int(end.timestamp() * 1000)),
            "limit": "100",
        }
        previous_oldest = None
        for _ in range(settings.max_pages_per_minute):
            data = await get(TRADE_PATH, params, deadline)
            pages += 1
            if not data:
                # An empty response is not proof of historical coverage, even if
                # the partially accumulated volume happens to equal the candle.
                fail("TRADE_SOURCE_EMPTY")
            ids, times = [], []
            if len(data) > 100:
                fail("TRADE_PAGE_INVALID")
            for row in data:
                ident, at = validate_trade(row, inst, client.now())
                ids.append(int(ident))
                times.append(at)
                if ident in seen and seen[ident] != row:
                    fail("TRADE_DUPLICATE_CONFLICT")
                seen[ident] = row
                if start <= at < end:
                    selected[ident] = row
            if ids != sorted(ids, reverse=True) or times != sorted(times, reverse=True):
                fail("TRADE_PAGE_ORDER")
            oldest = min(ids)
            if previous_oldest is not None and oldest >= previous_oldest:
                fail("TRADE_PAGINATION_STALLED")
            if min(times) < start:
                complete = True
                break
            previous_oldest = oldest
            params = {"instId": inst, "type": "1", "after": str(oldest), "limit": "100"}
        if not complete:
            fail("TRADE_PAGE_LIMIT")
        values = tuple(selected[k] for k in sorted(selected, key=int))
        totals = aggregate(values, face)
        if not verify_direction(values, face, totals):
            fail("TRADE_SIDE_CHECK_FAILED")
        if not same_volume(totals["buy"] + totals["sell"], candle.volume_base):
            fail("TRADE_CANDLE_VOLUME_MISMATCH")
        if not same_volume(candle.volume_contracts * face, candle.volume_base):
            fail("TRADE_CANDLE_UNIT_MISMATCH")
    except (DomainError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        reason = exc.code if isinstance(exc, DomainError) else "TRADE_SOURCE_INVALID"
    values = tuple(selected[k] for k in sorted(selected, key=int))
    payload = {
        "timeframe": "1m",
        "period": "1m",
        "reason": reason,
        "parser_version": 1,
        "trade_count": len(values),
        "raw_digest": content_hash(values),
        "pagination_complete": complete,
        "pages": pages,
        "first_trade_id": values[0]["tradeId"] if values else None,
        "last_trade_id": values[-1]["tradeId"] if values else None,
        "candle_volume": candle.volume_base if candle else None,
        "candle_hash": candle.market_content_hash if candle else None,
        "face_value": metadata.get("ctVal"),
        "direction_check": "RAW_SIDE_RECALCULATION" if totals else "UNAVAILABLE",
        **totals,
    }
    return MinuteCapture(
        Observation(
            source=MINUTE_SOURCE,
            inst_id=inst,
            kind="taker_volume",
            event_at=end,
            available_at=client.now(),
            coverage_start=start,
            coverage_end=end,
            unit="base_quantity",
            quality="MISSING" if reason else "OK",
            payload=payload,
        ),
        values,
        candle,
    )
