from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import INTERVAL_SECONDS, OKX_BARS, TIMEFRAMES, boundary
from trading_agent.market.models import Observation


def timestamp(value):
    if isinstance(value, bool) or not (
        type(value) is int or isinstance(value, str) and value.isdecimal()
    ):
        raise ValueError("invalid market timestamp")
    milliseconds = int(value)
    if milliseconds < 0:
        raise ValueError("negative market timestamp")
    seconds, remainder = divmod(milliseconds, 1000)
    try:
        return datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=remainder)
    except (ValueError, OverflowError, OSError):
        raise ValueError("invalid market timestamp range") from None


def number(value, *, nonnegative=False):
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid external market number") from None
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise ValueError("invalid external market number")
    return parsed


def parse_observations(source, inst_id, data, available_at):
    result = []
    for row in data:
        if not isinstance(row, dict) or row.get("instId") != inst_id:
            raise DomainError("OBSERVATION_INSTRUMENT_MISMATCH", "外部观测标的与任务合约不一致")
        if source == "open-interest":
            event_at = timestamp(row["ts"])
            payload = {
                "quantity": number(row["oi"], nonnegative=True),
                "base_quantity": number(row["oiCcy"], nonnegative=True),
                "usd_value": number(row["oiUsd"], nonnegative=True),
                "scope": "contract",
            }
            kind, unit = "oi", "contracts"
        elif source == "funding-rate-history":
            event_at = timestamp(row["fundingTime"])
            payload = {
                "event_type": "settled",
                "realized_rate": number(row["realizedRate"]),
                "predicted_rate": number(row["fundingRate"]),
            }
            kind, unit = "funding", "rate"
        elif source == "funding-rate":
            # fundingTime is a future settlement, not the observation's publication time.
            event_at = timestamp(row["ts"])
            payload = {
                "event_type": "published",
                "published_rate": number(row["fundingRate"]),
                "scheduled_funding_at": timestamp(row["fundingTime"]).isoformat(),
            }
            kind, unit = "funding", "rate"
        else:
            raise ValueError("unknown observation source")
        if event_at > available_at:
            raise DomainError("OBSERVATION_FUTURE_EVENT", "已发生观测的事件时间晚于实际获取时间")
        result.append(
            Observation(
                source="okx_public:" + source,
                inst_id=inst_id,
                kind=kind,
                event_at=event_at,
                available_at=available_at,
                unit=unit,
                payload=payload,
            )
        )
    return tuple(result)


STATISTICS = (
    ("taker-volume-contract", "taker_volume", "base_quantity", "taker_timestamp"),
    ("contracts/open-interest-history", "oi", "contracts", "oi_history_timestamp"),
    (
        "contracts/long-short-account-ratio-contract",
        "long_short_ratio",
        "ratio",
        "long_short_timestamp",
    ),
)


def parse_statistics(source, inst_id, timeframe, mode, data, available_at):
    """Keep the declared venue clock contract separate from local acquisition time."""
    seconds = INTERVAL_SECONDS[timeframe]
    seen, records = set(), []
    for row in data:
        expected = {
            "taker-volume-contract": 3,
            "contracts/open-interest-history": 4,
            "contracts/long-short-account-ratio-contract": 2,
        }[source]
        if not isinstance(row, list) or len(row) != expected:
            raise DomainError("STATISTICS_SCHEMA", "合约统计数组字段数不正确")
        reported = timestamp(row[0])
        if reported in seen:
            raise DomainError("DUPLICATE_OBSERVATION", "合约统计返回重复时间戳")
        seen.add(reported)
        if mode not in {"instant", "open", "close"} or (
            source == "taker-volume-contract" and mode == "instant"
        ):
            raise ValueError("statistics timestamp contract not selected")
        if mode != "instant" and reported.timestamp() % seconds:
            raise DomainError("STATISTICS_BOUNDARY", "合约统计区间未按UTC周期对齐")
        event_at = reported + timedelta(seconds=seconds) if mode == "open" else reported
        if event_at > available_at:
            continue  # This bucket is still open. It cannot become frozen historical evidence.
        payload = {
            "timeframe": timeframe,
            "venue_timestamp": reported.isoformat(),
            "timestamp_contract": mode,
            "period": OKX_BARS[timeframe],
        }
        coverage = {}
        if source == "taker-volume-contract":
            start = reported if mode == "open" else reported - timedelta(seconds=seconds)
            coverage = {"coverage_start": start, "coverage_end": event_at}
            # The venue returns SELL first, BUY second; unit=0 explicitly requests crypto.
            payload.update(
                sell=number(row[1], nonnegative=True),
                buy=number(row[2], nonnegative=True),
                venue_unit="0",
            )
            kind, unit = "taker_volume", "base_quantity"
        elif source == "contracts/open-interest-history":
            payload.update(
                quantity=number(row[1], nonnegative=True),
                base_quantity=number(row[2], nonnegative=True),
                usd_value=number(row[3], nonnegative=True),
                scope="contract",
            )
            kind, unit = "oi", "contracts"
        else:
            payload.update(ratio=number(row[1], nonnegative=True), scope="contract_accounts")
            kind, unit = "long_short_ratio", "ratio"
        records.append(
            Observation(
                source=f"okx_rubik:{source}:{timeframe}:{mode}:v1",
                inst_id=inst_id,
                kind=kind,
                event_at=event_at,
                available_at=available_at,
                unit=unit,
                payload=payload,
                **coverage,
            )
        )
    return tuple(records)


async def collect_statistics(client, deadline, *, timeframes=TIMEFRAMES, anchor=None):
    anchor = anchor or boundary(client.now())

    async def fetch(source, kind, unit, field, timeframe):
        mode = getattr(client.config, field)
        at = client.now()
        source_key = f"okx_rubik:{source}:{timeframe}:{mode or 'unverified'}:v1"
        reason = (
            "UNVERIFIED_TIMESTAMP_CONTRACT" if mode is None else "SOURCE_UNAVAILABLE_OR_INVALID"
        )
        if mode is not None:
            end = boundary(anchor, timeframe)
            count = 20 if kind == "taker_volume" else 2
            params = {
                "instId": client.config.inst_id,
                "period": OKX_BARS[timeframe],
                "begin": str(
                    int(
                        (end - timedelta(seconds=count * INTERVAL_SECONDS[timeframe])).timestamp()
                        * 1000
                    )
                    - 1
                ),
                "end": str(int(end.timestamp() * 1000) + 1),
                "limit": "100",
            }
            if kind == "taker_volume":
                params["unit"] = "0"
            try:
                data = await client.get("/api/v5/rubik/stat/" + source, params, deadline)
                parsed = parse_statistics(
                    source, client.config.inst_id, timeframe, mode, data, client.now()
                )
                if parsed:
                    return parsed
                reason = "SOURCE_EMPTY" if not data else "NO_COMPLETED_STATISTICS_BUCKET"
            except (DomainError, KeyError, ValueError, TypeError) as exc:
                reason = exc.code if isinstance(exc, DomainError) else "SOURCE_INVALID"
            at = client.now()
        return (
            Observation(
                source=source_key,
                inst_id=client.config.inst_id,
                kind=kind,
                event_at=at,
                available_at=at,
                unit=unit,
                quality="MISSING",
                payload={"reason": reason, "timeframe": timeframe},
            ),
        )

    batches = await asyncio.gather(
        *(
            fetch(*spec, tf)
            for spec in STATISTICS
            if spec[1] != "taker_volume" or client.config.taker_source == "rubik"
            for tf in timeframes
        )
    )
    return tuple(item for batch in batches for item in batch)


async def collect(client, deadline, *, timeframes=TIMEFRAMES, anchor=None):
    """Bound optional I/O and collect only the requested prediction periods."""
    client.config.require_instrument()
    from trading_agent.market.breadth import collect_breadth

    endpoints = [
        (
            "open-interest",
            "oi",
            "contracts",
            {
                "instId": client.config.inst_id,
                "instType": "SWAP" if client.config.contract_type == "perpetual" else "FUTURES",
            },
        )
    ]
    if client.config.contract_type == "perpetual":
        endpoints.extend(
            [
                ("funding-rate", "funding", "rate", {"instId": client.config.inst_id}),
                (
                    "funding-rate-history",
                    "funding",
                    "rate",
                    {"instId": client.config.inst_id, "limit": "100"},
                ),
            ]
        )
    optional_deadline = min(deadline, client.now() + timedelta(seconds=20))

    async def fetch(source, kind, unit, params):
        try:
            data = await client.get("/api/v5/public/" + source, params, optional_deadline)
            parsed = parse_observations(source, client.config.inst_id, data, client.now())
            if parsed:
                return parsed
            reason = "SOURCE_EMPTY"
        except (DomainError, KeyError, ValueError, TypeError) as exc:
            reason = exc.code if isinstance(exc, DomainError) else "SOURCE_INVALID"
        at = client.now()
        return (
            Observation(
                source="okx_public:" + source,
                inst_id=client.config.inst_id,
                kind=kind,
                event_at=at,
                available_at=at,
                unit=unit,
                quality="MISSING",
                payload={"reason": reason},
            ),
        )

    batches = await asyncio.gather(
        *(fetch(*endpoint) for endpoint in endpoints),
        collect_statistics(client, optional_deadline, timeframes=timeframes, anchor=anchor),
        collect_breadth(client, optional_deadline, timeframes=timeframes, anchor=anchor),
    )
    return tuple(item for batch in batches for item in batch)
