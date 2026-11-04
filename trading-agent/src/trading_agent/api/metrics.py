"""Operational measurements from stored timestamps and native usage; unknown is not zero."""

from datetime import timedelta

import sqlalchemy as sa

from trading_agent.storage import schema as s


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"sample_count": 0, "p50_seconds": None, "p95_seconds": None}

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        index = int(position)
        following = min(index + 1, len(ordered) - 1)
        return ordered[index] + (ordered[following] - ordered[index]) * (position - index)

    return {
        "sample_count": len(ordered),
        "p50_seconds": percentile(0.5),
        "p95_seconds": percentile(0.95),
    }


def run_metrics(rows):
    groups = {role: [] for role in ("parent", "1h", "4h", "6h", "12h")}
    recorded_attempts = 0
    for row in rows:
        runs = (row["usage_json"] or {}).get("model_runs", {})
        recorded_attempts += bool(runs)
        for run in runs.values():
            if run["role"] in groups:
                groups[run["role"]].append(run)
    result = {}
    for role, runs in groups.items():
        terminal = [run for run in runs if run["status"] not in {"running", "unknown"}]
        failed = sum(run["status"] != "completed" for run in terminal)
        summary = {
            "reported_sessions": len(runs),
            "terminal_sessions": len(terminal),
            "failed_sessions": failed,
            "failure_rate": failed / len(terminal) if terminal else None,
            **percentiles(
                [
                    float(run["duration_seconds"])
                    for run in runs
                    if run.get("duration_seconds") is not None
                ]
            ),
        }
        for key in ("api_calls", "input_tokens", "output_tokens"):
            values = [run[key] for run in runs if run.get(key) is not None]
            summary[key] = {"known_sessions": len(values), "total": sum(values) if values else None}
        result[role] = summary
    return {
        "attempts_sampled": len(rows),
        "attempts_with_usage": recorded_attempts,
        "roles": result,
    }


async def performance(connection, now):
    since = now - timedelta(hours=24)
    elapsed = sa.extract("epoch", s.forecasts.c.published_at - s.job_attempts.c.model_started_at)
    elapsed = sa.case((elapsed >= 0, elapsed))
    forecast_rows = (
        (
            await connection.execute(
                sa.select(
                    s.forecasts.c.engine_id,
                    sa.func.count().label("published"),
                    sa.func.count(elapsed).label("sample_count"),
                    sa.func.percentile_cont(0.5).within_group(elapsed).label("p50_seconds"),
                    sa.func.percentile_cont(0.95).within_group(elapsed).label("p95_seconds"),
                )
                .select_from(
                    s.forecasts.join(
                        s.timeframe_analyses,
                        s.forecasts.c.one_hour_analysis_id == s.timeframe_analyses.c.id,
                    ).join(s.job_attempts, s.timeframe_analyses.c.attempt_id == s.job_attempts.c.id)
                )
                .where(s.forecasts.c.published_at >= since, s.forecasts.c.published_at <= now)
                .group_by(s.forecasts.c.engine_id)
            )
        )
        .mappings()
        .all()
    )
    first = (
        sa.select(
            s.settlements.c.forecast_id, sa.func.min(s.settlements.c.settled_at).label("settled_at")
        )
        .group_by(s.settlements.c.forecast_id)
        .subquery()
    )
    lag = sa.extract(
        "epoch", first.c.settled_at - (s.forecasts.c.end_at + sa.text("interval '1 minute'"))
    )
    lag = sa.case((lag >= 0, lag))
    settlement = (
        (
            await connection.execute(
                sa.select(
                    sa.func.count(lag).label("sample_count"),
                    sa.func.percentile_cont(0.5).within_group(lag).label("p50_seconds"),
                    sa.func.percentile_cont(0.95).within_group(lag).label("p95_seconds"),
                )
                .select_from(first.join(s.forecasts, first.c.forecast_id == s.forecasts.c.id))
                .where(first.c.settled_at >= since, first.c.settled_at <= now)
            )
        )
        .mappings()
        .one()
    )
    usage_rows = (
        (
            await connection.execute(
                sa.select(s.job_attempts.c.usage_json)
                .join(s.jobs, s.jobs.c.id == s.job_attempts.c.job_id)
                .where(
                    s.job_attempts.c.started_at >= since,
                    s.job_attempts.c.started_at <= now,
                    s.jobs.c.kind.in_(("FORECAST", "EXPLAIN", "REVIEW_DAILY")),
                )
                .order_by(s.job_attempts.c.started_at.desc(), s.job_attempts.c.id)
                .limit(2001)
            )
        )
        .mappings()
        .all()
    )
    # Count reuse of frozen long reports from another attempt, including a same-job retry.
    cache = (
        (
            await connection.execute(
                sa.select(
                    sa.func.count().label("references"),
                    sa.func.count()
                    .filter(
                        s.trend_analyses.c.producer_attempt_id != s.timeframe_analyses.c.attempt_id
                    )
                    .label("reused"),
                )
                .select_from(
                    s.forecasts.join(
                        s.forecast_trends, s.forecasts.c.id == s.forecast_trends.c.forecast_id
                    )
                    .join(
                        s.trend_analyses,
                        s.forecast_trends.c.trend_id == s.trend_analyses.c.id,
                    )
                    .join(
                        s.timeframe_analyses,
                        s.forecasts.c.one_hour_analysis_id == s.timeframe_analyses.c.id,
                    )
                )
                .where(s.forecasts.c.published_at >= since, s.forecasts.c.published_at <= now)
            )
        )
        .mappings()
        .one()
    )
    return {
        "window_start": since,
        "window_end": now,
        "forecast_from_model_start": [dict(row) for row in forecast_rows],
        "first_settlement_after_endpoint_close": dict(settlement),
        "long_trend_cache": dict(cache),
        "native_models": {
            **run_metrics(usage_rows[:2000]),
            "sample_truncated": len(usage_rows) > 2000,
            "usage_scope": "native reported counters; parent last sampled before submission",
        },
    }
