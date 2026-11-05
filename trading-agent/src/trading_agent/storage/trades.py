"""Read immutable minute revisions as of a prediction's guarded freeze time."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import INTERVAL_SECONDS, boundary
from trading_agent.market.models import Observation
from trading_agent.market.trades import MINUTE_SOURCE
from trading_agent.storage import schema as s


async def trade_windows(connection, inst_id, timeframe, end, decision_at):
    step = timedelta(seconds=INTERVAL_SECONDS[timeframe])
    start = end - 20 * step
    table = s.market_observations
    rows = (
        (
            await connection.execute(
                sa.select(table)
                .where(
                    table.c.source == MINUTE_SOURCE,
                    table.c.inst_id == inst_id,
                    table.c.kind == "taker_volume",
                    table.c.event_at > start,
                    table.c.event_at <= end,
                    table.c.available_at <= decision_at,
                )
                .distinct(table.c.event_at)
                .order_by(table.c.event_at, table.c.revision_no.desc())
            )
        )
        .mappings()
        .all()
    )
    by_end = {row["event_at"]: row for row in rows}
    result = []
    minutes = int(step.total_seconds() // 60)
    for index in range(20):
        left, right = start + index * step, start + (index + 1) * step
        expected = [left + timedelta(minutes=m + 1) for m in range(minutes)]
        group = [by_end[t] for t in expected if t in by_end]
        good = [
            r
            for r in group
            if r["quality"] == "OK"
            and r["coverage_start"] == r["event_at"] - timedelta(minutes=1)
            and r["coverage_end"] == r["event_at"]
            and r["unit"] == "base_quantity"
        ]
        complete = len(good) == minutes
        payload = {
            "timeframe": timeframe,
            "aggregation": "VERIFIED_MINUTES",
            "minute_source": MINUTE_SOURCE,
            "expected_minutes": minutes,
            "verified_minutes": len(good),
            "minute_versions_hash": content_hash(
                [(r["id"], r["revision_no"], r["payload_hash"]) for r in group]
            ),
            "minutes_as_of": max((r["available_at"] for r in group), default=decision_at),
            "reason": None if complete else "TRADE_MINUTES_INCOMPLETE",
        }
        if complete:
            payload.update(
                buy=sum((Decimal(str(r["payload"]["buy"])) for r in good), Decimal(0)),
                sell=sum((Decimal(str(r["payload"]["sell"])) for r in good), Decimal(0)),
            )
        result.append(
            Observation(
                source=f"okx_trade_window:{timeframe}:v1",
                inst_id=inst_id,
                kind="taker_volume",
                event_at=right,
                available_at=max(right, payload["minutes_as_of"]),
                coverage_start=left,
                coverage_end=right,
                unit="base_quantity",
                quality="OK" if complete else "MISSING",
                payload=payload,
            )
        )
    return tuple(result)


async def coverage_status(connection, inst_ids, now):
    """Read the same as-of minute selection used by formal prediction freezing."""
    report = {"as_of": now.isoformat(), "instruments": {}}
    for inst in inst_ids:
        periods = {}
        for tf in ("1h", "4h", "6h", "12h"):
            end = boundary(now, tf)
            windows = await trade_windows(connection, inst, tf, end, now)
            expected = sum(w.payload["expected_minutes"] for w in windows)
            verified = sum(w.payload["verified_minutes"] for w in windows)
            periods[tf] = {
                "start": windows[0].coverage_start.isoformat(),
                "end": end.isoformat(),
                "expected_minutes": expected,
                "verified_minutes": verified,
                "missing_minutes": expected - verified,
                "ready": verified == expected,
                "incomplete_intervals": [
                    {
                        "start": w.coverage_start.isoformat(),
                        "end": w.coverage_end.isoformat(),
                        "missing_minutes": w.payload["expected_minutes"]
                        - w.payload["verified_minutes"],
                    }
                    for w in windows
                    if w.quality != "OK"
                ],
            }
        work = s.trade_minute_work
        failures = (
            (
                await connection.execute(
                    sa.select(work.c.open_at, work.c.reason, work.c.attempts, work.c.next_retry_at)
                    .where(work.c.inst_id == inst, work.c.state == "ERROR")
                    .order_by(work.c.updated_at.desc())
                    .limit(10)
                )
            )
            .mappings()
            .all()
        )
        report["instruments"][inst] = {
            "periods": periods,
            "recent_errors": [
                {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()}
                for row in failures
            ],
        }
    return report
