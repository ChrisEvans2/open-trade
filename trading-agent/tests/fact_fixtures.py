"""Schema fixtures for the current runtime's historical read/retirement contract.

These synthetic rows represent stored history. No legacy researcher, scorer, scheduler
or archived code is imported or executed.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
from conftest import bars_at

from trading_agent.domain.serialization import content_hash
from trading_agent.market.snapshots import build_snapshot
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar, persist_snapshot


async def historical_forecast(system, *, with_score=False):
    database, clock, jobs, service, engine = system
    anchor = clock.value.replace(minute=0, second=0, microsecond=0)
    async with database.transaction() as connection:
        await jobs.ownership.guard(connection)
        await jobs.create(
            connection, "FORECAST", "fixture-history:" + str(uuid4()), {}, clock.value
        )
    attempt = await jobs.claim(("FORECAST",))
    payload = {"p_up": "0.6", "p_flat": "0.3", "p_down": "0.1", "fixture": "stored-history"}
    bundle, analysis, forecast = uuid4(), uuid4(), uuid4()
    start, end = anchor + timedelta(minutes=5), anchor + timedelta(minutes=65)
    score = None
    async with jobs.transaction(attempt) as connection:
        data = {}
        for timeframe in ("1h", "4h", "6h", "12h"):
            data[timeframe] = tuple(
                [await persist_bar(connection, bar) for bar in bars_at(anchor, timeframe)]
            )
        snapshot = build_snapshot(anchor, clock.value, data, service.config.features)
        snapshot_id = await persist_snapshot(connection, snapshot)
        await connection.execute(
            sa.insert(s.context_bundles).values(
                id=bundle,
                job_id=attempt.job_id,
                snapshot_id=snapshot_id,
                evidence_ids=[],
                observation_ids=[],
                trend_analysis_ids={},
                stats={},
                rendered_text="fixture history",
                content_hash=content_hash(str(bundle)),
            )
        )
        await connection.execute(
            sa.insert(s.timeframe_analyses).values(
                id=analysis,
                attempt_id=attempt.id,
                bundle_id=bundle,
                timeframe="1h",
                hermes_child_id="fixture-stored-child",
                p_up=Decimal("0.6"),
                p_flat=Decimal("0.3"),
                p_down=Decimal("0.1"),
                reason="fixture history",
                risk_notes=[],
                evidence_ids=[],
                schema_valid=True,
                payload=payload,
                payload_hash=content_hash(payload),
            )
        )
        await connection.execute(
            sa.insert(s.forecasts).values(
                id=forecast,
                job_id=attempt.job_id,
                symbol="BTCUSDT",
                anchor_at=anchor,
                start_at=start,
                end_at=end,
                engine_id=engine,
                strategy_id=None,
                bundle_id=bundle,
                one_hour_analysis_id=analysis,
                trend_analysis_ids={},
                p_up=Decimal("0.6"),
                p_flat=Decimal("0.3"),
                p_down=Decimal("0.1"),
                label="UP",
                reason="fixture history",
                risk_notes=[],
                evidence_ids=[],
                payload=payload,
                submission_payload=payload,
                submission_hash=content_hash(payload),
                payload_hash=content_hash(payload),
                published_at=clock.value,
            )
        )
        await connection.execute(
            sa.insert(s.audit_events).values(
                actor_type="fixture",
                actor_id="test",
                action="STORED_HISTORY",
                object_type="forecast",
                object_id=forecast,
                request_id=str(uuid4()),
            )
        )
        if with_score:
            from test_prediction_pipeline import minute

            first = await persist_bar(
                connection, minute("BTC-USDT-SWAP", start, high="100", low="100", close="100")
            )
            last = await persist_bar(
                connection, minute("BTC-USDT-SWAP", end, high="101", low="101", close="101")
            )
            score = uuid4()
            await connection.execute(
                sa.insert(s.settlements).values(
                    id=score,
                    forecast_id=forecast,
                    revision_no=1,
                    start_bar_id=first.id,
                    end_bar_id=last.id,
                    realized_return=Decimal("0.01"),
                    actual_label="UP",
                    correct=True,
                    brier=Decimal("0.26"),
                    payload_hash=content_hash(str(score)),
                    settled_at=end + timedelta(minutes=1),
                )
            )
    if with_score:
        clock.value = end + timedelta(minutes=1)
    return SimpleNamespace(id=forecast, attempt=attempt, payload=payload, score_id=score)
