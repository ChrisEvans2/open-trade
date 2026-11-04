"""Select immutable case inputs and attach separately labeled reconstruction evidence."""

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import boundary, utc
from trading_agent.market.reconstruction import reconstruct_input
from trading_agent.storage import schema as s
from trading_agent.storage.market import row_bar


def review_range(attempt, now):
    start = (
        utc(datetime.fromisoformat(attempt.payload["range_start"]))
        if "range_start" in attempt.payload
        else now - timedelta(days=90)
    )
    end = (
        utc(datetime.fromisoformat(attempt.payload["range_end"]))
        if "range_end" in attempt.payload
        else now
    )
    if not timedelta(0) < end - start <= timedelta(days=90) or end > now:
        raise DomainError("REVIEW_RANGE", "复盘范围必须非空、不含未来且跨度不超过90天", 422)
    return start, end


async def select_cases(connection, attempt, engine_id, as_of):
    start, end = review_range(attempt, as_of)
    # Indexed lateral lookup retains the latest evaluation VISIBLE at this review's cutoff.
    evaluations = (
        sa.select(s.prediction_evaluations)
        .where(
            s.prediction_evaluations.c.prediction_id == s.prediction_facts.c.id,
            s.prediction_evaluations.c.evaluated_at <= as_of,
        )
        .order_by(s.prediction_evaluations.c.revision.desc())
        .limit(1)
        .correlate(s.prediction_facts)
        .lateral()
    )
    rows = (
        (
            await connection.execute(
                sa.select(
                    s.prediction_facts.c.id.label("prediction_id"),
                    s.prediction_facts.c.body.label("prediction"),
                    evaluations.c.id.label("evaluation_id"),
                    evaluations.c.body.label("evaluation"),
                    s.prediction_inputs.c.id.label("original_input_id"),
                    s.prediction_inputs.c.body.label("original_body"),
                    s.prediction_inputs.c.content_hash.label("original_content_hash"),
                    s.prediction_inputs.c.decision_at.label("original_decision_at"),
                    s.prediction_inputs.c.timeframe.label("original_timeframe"),
                )
                .select_from(s.prediction_facts)
                .join(evaluations, sa.true())
                .join(
                    s.prediction_inputs, s.prediction_inputs.c.id == s.prediction_facts.c.input_id
                )
                .where(
                    s.prediction_facts.c.engine_id == engine_id,
                    s.prediction_facts.c.symbol == attempt.payload["symbol"],
                    s.prediction_facts.c.published_at >= start,
                    s.prediction_facts.c.published_at < end
                    if "range_end" in attempt.payload
                    else s.prediction_facts.c.published_at <= end,
                )
                .order_by(evaluations.c.evaluated_at.desc(), s.prediction_facts.c.id)
                .limit(200)
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            **{
                key: row[key]
                for key in ("prediction_id", "prediction", "evaluation_id", "evaluation")
            },
            "original_input": {
                "id": row["original_input_id"],
                "body": row["original_body"],
                "content_hash": row["original_content_hash"],
                "decision_at": row["original_decision_at"],
                "timeframe": row["original_timeframe"],
            },
        }
        for row in rows
    ]


async def collect_minutes(cases, client, deadline):
    """Bounded public I/O; caller must have released its guarded transaction."""
    collected, windows = {}, {}
    for case in cases:
        original = case["original_input"]
        if (
            original["body"].get("forming_candle") is not None
            or original["body"].get("input_kind") == "CHILD_SYNTHESIS"
        ):
            continue
        target, tf = original["decision_at"], original["timeframe"]
        start, end = boundary(target, tf), boundary(target, "1m")
        key = (original["body"]["inst_id"], start, end)
        if key not in windows:
            try:
                windows[key] = (
                    {
                        "minutes": await client.minute_window(
                            key[0], start, end, deadline, max_pages=3
                        )
                    }
                    if start < end
                    else {"error": "NO_COMPLETED_MINUTE"}
                )
            except DomainError as exc:
                # API/data errors are visible gaps; internal programming errors still propagate.
                windows[key] = {"error": exc.code}
        collected[str(original["id"])] = windows[key]
    return collected


async def build_case_inputs(connection, cases, historical_minutes, config, now):
    needed_ids = {
        UUID(ident)
        for case in cases
        if case["original_input"]["body"].get("forming_candle") is None
        for ident in case["original_input"]["body"].get("bar_ids", [])
    }
    rows = (
        (
            await connection.execute(
                sa.select(s.market_bars).where(s.market_bars.c.id.in_(needed_ids))
            )
        )
        .mappings()
        .all()
        if needed_ids
        else []
    )
    original_bars = {str(row["id"]): row_bar(row) for row in rows}
    details, summaries = {}, []
    for case in cases:
        original = case["original_input"]
        body = original["body"]
        if content_hash(body) != original["content_hash"]:
            raise DomainError("ORIGINAL_INPUT_HASH", "原预测输入校验失败", 409)
        reconstruction = None
        if body.get("input_kind") == "CHILD_SYNTHESIS":
            evidence = {"kind": "CHILD_SYNTHESIS", "quality": "NOT_APPLICABLE"}
        elif body.get("forming_candle") is not None:
            evidence = {"kind": "ORIGINAL_SNAPSHOT", "quality": "OK"}
        else:
            prepared = historical_minutes.get(str(original["id"]), {})
            try:
                if "minutes" not in prepared:
                    raise DomainError(prepared.get("error", "NOT_PREPARED"), "未取得重建分钟数据")
                closed = tuple(
                    original_bars[ident]
                    for ident in body.get("bar_ids", [])
                    if ident in original_bars
                )
                reconstruction = reconstruct_input(
                    original,
                    closed,
                    prepared["minutes"],
                    config,
                    reconstructed_at=now,
                )
                evidence = {
                    "kind": "RECONSTRUCTED_FROM_1M",
                    "quality": reconstruction["quality"],
                    "data_cutoff": reconstruction["forming_candle"]["data_cutoff"],
                    "uncovered_seconds": reconstruction["uncovered_seconds"],
                }
            except DomainError as exc:
                reconstruction = {
                    "quality": "MISSING",
                    "missing_reason": exc.code,
                    "evidence_kind": "RECONSTRUCTED_FROM_1M",
                }
                evidence = {"kind": "MISSING", "quality": "MISSING", "missing_reason": exc.code}
        details[str(case["prediction_id"])] = {**case, "candle_reconstruction": reconstruction}
        summaries.append(
            {
                **{key: value for key, value in case.items() if key != "original_input"},
                "original_input": {key: value for key, value in original.items() if key != "body"},
                "candle_evidence": evidence,
            }
        )
    return summaries, details
