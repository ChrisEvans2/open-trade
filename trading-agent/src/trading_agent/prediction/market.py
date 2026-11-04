"""Public market collection, durable minute cursor and structural position protection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from trading_quant.structures import detect_structures

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import TIMEFRAMES, boundary
from trading_agent.prediction.contracts import PublishedPrediction
from trading_agent.prediction.evaluation import EvaluationState, condition_matches
from trading_agent.prediction.service import instrument, scope_filter
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar


class PredictionMarket:
    def __init__(self, jobs, client, predictions, knowledge, engine_id, *, sampler=None):
        self.jobs, self.database, self.client = jobs, jobs.database, client
        self.predictions, self.knowledge, self.engine_id = predictions, knowledge, engine_id
        self.sampler = sampler

    async def prepare(self, attempt):
        if attempt.payload.get("engine_id") != str(self.engine_id):
            raise DomainError("ENGINE_CHANGED", "任务版本与当前部署不一致", 409)
        if attempt.kind == "PREDICT_REVIEW" and attempt.payload.get("request_kind") == "explain":
            return await self.knowledge.freeze_explanation(attempt, self.engine_id)
        async with self.jobs.transaction(attempt) as connection:
            frozen = (
                await connection.execute(
                    sa.select(s.prediction_inputs.c.id, s.prediction_inputs.c.decision_at).where(
                        s.prediction_inputs.c.job_id == attempt.job_id
                    )
                )
            ).first()
            now = await self.database.now(connection)
            if attempt.kind in {"PREDICT_PARENT", "PREDICT_TIMEFRAME"} and not attempt.payload.get(
                "analysis_managed"
            ):
                covered = (
                    await connection.execute(
                        sa.select(s.prediction_inputs.c.body)
                        .select_from(s.prediction_heads)
                        .join(
                            s.prediction_facts,
                            s.prediction_facts.c.id == s.prediction_heads.c.prediction_id,
                        )
                        .join(
                            s.prediction_inputs,
                            s.prediction_inputs.c.id == s.prediction_facts.c.input_id,
                        )
                        .join(
                            s.prediction_states,
                            s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                        )
                        .where(
                            scope_filter(
                                s.prediction_heads,
                                self.engine_id,
                                attempt.payload["symbol"],
                                attempt.payload.get("timeframe", "1h"),
                                "PARENT" if attempt.kind == "PREDICT_PARENT" else "TIMEFRAME",
                            ),
                            s.prediction_facts.c.job_id != attempt.job_id,
                            s.prediction_facts.c.expires_at > now,
                            s.prediction_states.c.body["lifecycle"].as_string() == "ACTIVE",
                            s.prediction_states.c.data_quality == "OK",
                            s.prediction_inputs.c.decision_at
                            >= sa.select(s.jobs.c.created_at)
                            .where(s.jobs.c.id == attempt.job_id)
                            .scalar_subquery(),
                        )
                    )
                ).scalar_one_or_none()
                if covered is not None and attempt.kind == "PREDICT_PARENT":
                    _, _, revision = await self.predictions._children(
                        connection, self.engine_id, attempt.payload["symbol"], now
                    )
                    if revision != covered["context_revision"]:
                        covered = None
                if covered is not None:
                    raise DomainError(
                        "ANALYSIS_SUPERSEDED", "已有更新且仍有效的预测覆盖此排队请求", 409
                    )
            if frozen:
                # A retry needs room for sandbox preparation and the unchanged
                # full model budget; merely being fresh at launch is insufficient.
                required_until = now + timedelta(
                    seconds=self.predictions.config.runtime.agent_timeout_seconds + 20
                )
                if attempt.kind in {"PREDICT_TIMEFRAME", "PREDICT_PARENT"} and (
                    required_until
                    > frozen.decision_at
                    + timedelta(seconds=self.predictions.config.prediction.context_max_age_seconds)
                ):
                    raise DomainError("CONTEXT_EXPIRED", "冻结行情已过时，需要重新分析", 409)
                return frozen.id
            if attempt.kind == "PREDICT_PARENT":
                stream = await connection.scalar(
                    sa.select(s.prediction_streams.c.body).where(
                        scope_filter(
                            s.prediction_streams, self.engine_id, attempt.payload["symbol"]
                        )
                    )
                )
                if stream and stream.get("data_quality") == "AWAITING_DATA":
                    # A just-closed candle is still being filled. Starting a
                    # synthesis now would omit children that reappear moments later.
                    raise DomainError(
                        "MINUTE_DATA_PENDING", "分钟行情待补齐，暂不启动综合分析", 503
                    )
        if attempt.kind == "PREDICT_PARENT":
            ident = await self.predictions.freeze_parent(attempt, self.engine_id)
            await self.knowledge.search(attempt)
            return ident
        symbol, timeframe = attempt.payload["symbol"], attempt.payload.get("timeframe", "1h")
        deadline = now + timedelta(seconds=40)
        await self.client.check_clock(deadline)
        metadata = await self.client.instrument(deadline, inst_id=instrument(symbol))
        if attempt.kind == "PREDICT_REVIEW":
            anchor = boundary(now, "1h")
            groups = await asyncio.gather(
                *(
                    self.client.candles(anchor, tf, deadline, inst_id=instrument(symbol))
                    for tf in TIMEFRAMES
                )
            )
            history = await self.knowledge.collect_review_minutes(
                attempt, self.engine_id, self.client, deadline
            )
            return await self.knowledge.freeze_review(
                attempt,
                self.engine_id,
                dict(zip(TIMEFRAMES, groups, strict=True)),
                anchor=anchor,
                history=history,
            )
        bars = await self.client.candles(
            boundary(now, "1h"), timeframe, deadline, inst_id=instrument(symbol)
        )
        observations = await self.client.observations(
            deadline,
            inst_id=instrument(symbol),
            symbol=symbol,
            timeframes=(timeframe,),
            anchor=boundary(now, "1h"),
        )
        benchmark, benchmark_reason = (), "NOT_APPLICABLE_SELF_BTC"
        if symbol == "ETHUSDT":
            from trading_agent.market.snapshots import validate_bars

            try:
                benchmark = await self.client.candles(
                    boundary(now, "1h"), timeframe, deadline, inst_id="BTC-USDT-SWAP"
                )
                validate_bars(benchmark, boundary(now, "1h"), timeframe, self.client.now())
                benchmark_reason = None
            except DomainError as exc:
                benchmark, benchmark_reason = (), exc.code
        forming, missing_reason = None, "CURRENT_CANDLE_NOT_RETURNED"
        try:
            forming = await self.client.forming_candle(
                timeframe, deadline, inst_id=instrument(symbol)
            )
        except DomainError as exc:
            if exc.code not in {
                "MARKET_DEADLINE",
                "MARKET_UNAVAILABLE",
                "MARKET_REJECTED",
                "MARKET_SCHEMA",
                "INVALID_FORMING_CANDLE",
                "FORMING_CANDLE_BOUNDARY",
            }:
                raise
            # Optional live context never fabricates a price or hides its absence.
            missing_reason = exc.code
        ident = await self.predictions.freeze(
            attempt,
            self.engine_id,
            bars,
            observations,
            forming_candle=forming,
            forming_candle_missing_reason=missing_reason,
            benchmark_bars=benchmark,
            benchmark_missing_reason=benchmark_reason,
            liquidation_sampler=self.sampler,
            instrument_metadata=metadata,
        )
        # An initial retrieval is visible even if the model fails to search; prompts then
        # request narrower structure/stage queries and case/procedure expansion as needed.
        await self.knowledge.search(attempt)
        return ident

    async def verify(self, attempt, *, coordinator=False):
        symbol = attempt.payload.get("symbol")
        trusted = (
            coordinator
            and attempt.kind == "PREDICT_PARENT"
            and attempt.payload.get("analysis_round")
            and not attempt.payload.get("analysis_managed")
        )
        if (attempt.kind != "PREDICT_VERIFY" and not trusted) or attempt.payload.get(
            "engine_id"
        ) != str(self.engine_id):
            raise DomainError("FORBIDDEN", "验证任务范围不匹配", 403)
        inst = instrument(symbol)
        clause = scope_filter(s.prediction_streams, self.engine_id, symbol)
        async with self.jobs.transaction(attempt) as connection:
            now = await self.database.now(connection)
            row = (
                (await connection.execute(sa.select(s.prediction_streams).where(clause)))
                .mappings()
                .first()
            )
            earliest = await connection.scalar(
                sa.select(sa.func.min(s.prediction_facts.c.published_at)).where(
                    s.prediction_facts.c.engine_id == self.engine_id,
                    s.prediction_facts.c.symbol == symbol,
                )
            )
            start = row["cursor"] if row else boundary(earliest or now - timedelta(minutes=5), "1m")
            end = boundary(now, "1m")
        fetched = await self.client.minute_window(inst, start, end, now + timedelta(seconds=40))
        async with self.jobs.transaction(attempt) as connection:
            await self.predictions._lock_symbol(connection, self.engine_id, symbol)
            _, _, now = await self.jobs.guard(connection, attempt)
            # Validate the full batch BEFORE saving any source fact.
            if any(
                b.inst_id != inst
                or b.timeframe != "1m"
                or b.available_at > now
                or b.close_boundary > end
                or b.open_at < start
                for b in fetched
            ) or any(a.open_at >= b.open_at for a, b in zip(fetched, fetched[1:], strict=False)):
                raise DomainError("MINUTE_SCOPE", "补齐数据的范围、顺序或可用时点无效")
            for bar in fetched:
                await persist_bar(connection, bar, symbol)
        bars = await self.predictions.minute_bars(inst, start, end, now)
        cursor = start
        for bar in bars:
            if bar.open_at != cursor:
                break
            cursor = bar.close_boundary
        async with self.jobs.transaction(attempt) as connection:
            await self.predictions._lock_symbol(connection, self.engine_id, symbol)
            await self.jobs.guard(connection, attempt)
            current = (
                (await connection.execute(sa.select(s.prediction_streams).where(clause)))
                .mappings()
                .first()
            )
            if current and current["cursor"] != start:
                raise DomainError("CURSOR_CHANGED", "同币种分钟游标已被其他任务推进", 409)
            recovering = bool(current and current["body"].get("data_quality") == "AWAITING_DATA")
            body = {
                **(current["body"] if current else {}),
                "data_quality": "OK" if cursor >= end else "AWAITING_DATA",
                "missing_at": json_value(cursor) if cursor < end else None,
            }
            await connection.execute(
                insert(s.prediction_streams)
                .values(
                    engine_id=self.engine_id,
                    symbol=symbol,
                    cursor=cursor,
                    body=body,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[
                        s.prediction_streams.c.engine_id,
                        s.prediction_streams.c.symbol,
                    ],
                    set_={"cursor": cursor, "body": body, "updated_at": now},
                )
            )
            records = (
                (
                    await connection.execute(
                        sa.select(
                            s.prediction_facts.c.id,
                            s.prediction_facts.c.body,
                            s.prediction_states.c.body.label("state"),
                        )
                        .join(
                            s.prediction_states,
                            s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                        )
                        .where(
                            s.prediction_facts.c.engine_id == self.engine_id,
                            s.prediction_facts.c.symbol == symbol,
                            ~s.prediction_states.c.body["observation_complete"].as_boolean(),
                        )
                        .order_by(s.prediction_facts.c.published_at)
                        .limit(200)
                    )
                )
                .mappings()
                .all()
            )
        for record in records:
            prediction = PublishedPrediction.model_validate(record["body"]["prediction"])
            state = EvaluationState.model_validate(record["state"])
            evidence = await self.predictions.minute_bars(
                inst, state.verified_until, min(end, boundary(prediction.expires_at, "1m")), now
            )
            await self.predictions.verify(
                attempt, record["id"], evidence, coordinator=bool(trusted)
            )
        if cursor >= end:
            if recovering and not getattr(self.predictions, "rounds", None):
                # Compare only after the whole verification batch is complete.
                # An unchanged recovered context needs no additional model run.
                async with self.jobs.transaction(attempt) as connection:
                    await self.predictions._lock_symbol(connection, self.engine_id, symbol)
                    children, position, _ = await self.predictions._children(
                        connection, self.engine_id, symbol, now
                    )
                    parent_context = await connection.scalar(
                        sa.select(s.prediction_inputs.c.body)
                        .join(
                            s.prediction_facts,
                            s.prediction_facts.c.input_id == s.prediction_inputs.c.id,
                        )
                        .join(
                            s.prediction_heads,
                            s.prediction_heads.c.prediction_id == s.prediction_facts.c.id,
                        )
                        .where(
                            scope_filter(s.prediction_heads, self.engine_id, symbol, role="PARENT")
                        )
                    )

                    def holding_key(value):
                        if value is None:
                            return None
                        return (
                            str(value["position_id"]),
                            value["status"],
                            value["revision"],
                            value["body"].get("contracts"),
                            value["body"].get("direction"),
                        )

                    # A HOLD can replace its own monitor binding and timestamp.
                    # Those changes are not a new trading context. Publication
                    # still checks the full concurrency revision independently.
                    if parent_context is not None and (
                        {c["id"] for c in parent_context["children"]} != {c["id"] for c in children}
                        or holding_key(parent_context["position"]) != holding_key(position)
                    ):
                        await self.predictions.queue_parent(connection, self.engine_id, symbol, now)
            await self._holding_structure(attempt, symbol, end, now)
            if not getattr(self.predictions, "rounds", None):
                await self._stage_changes(attempt, symbol, now)
        return {
            "cursor": json_value(cursor),
            "data_quality": body["data_quality"],
            "verified_predictions": len(records),
        }

    async def _holding_structure(self, attempt, symbol, end, now):
        async with self.jobs.transaction(attempt) as connection:
            position = (
                (
                    await connection.execute(
                        sa.select(s.trading_positions).where(
                            scope_filter(s.trading_positions, self.engine_id, symbol)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                not position
                or position["status"] != "OPEN"
                or not position["body"].get("structure_prediction_id")
            ):
                return
            ident = UUID(position["body"]["structure_prediction_id"])
            payload = await connection.scalar(
                sa.select(s.prediction_facts.c.body).where(s.prediction_facts.c.id == ident)
            )
            if payload is None:
                raise DomainError("POSITION_FORECAST_MISSING", "持仓绑定的原预测不存在", 503)
            prediction = PublishedPrediction.model_validate(payload["prediction"])
        scenario = next(
            (
                item
                for item in prediction.report.scenarios
                if item.direction == position["body"]["direction"]
            ),
            None,
        )
        if scenario is None:
            return
        # Keep a separate holding cursor after the forecast's scoring window ends.
        # Replay every missed minute, so a breach followed by recovery is not forgotten.
        start = datetime.fromisoformat(
            position["body"].get("structure_verified_until")
            or position["body"].get("opened_at")
            or prediction.observation_start.isoformat()
        )
        start = max(boundary(start, "1m"), prediction.observation_start)
        overlap = timedelta(minutes=scenario.invalidation.confirmation_bars - 1)
        bars = await self.predictions.minute_bars(
            prediction.inst_id, max(prediction.observation_start, start - overlap), end, now
        )
        cursor, breached = start, position["body"].get("structure_breached_at")
        for index, bar in enumerate(bars):
            if bar.open_at < cursor:
                continue
            if bar.open_at != cursor:
                break
            cursor = bar.close_boundary
            if breached is None and condition_matches(scenario.invalidation, bars[: index + 1]):
                breached = json_value(bar.close_boundary)
        async with self.jobs.transaction(attempt) as connection:
            await self.predictions._lock_symbol(connection, self.engine_id, symbol)
            _, _, now = await self.jobs.guard(connection, attempt)
            current = (
                (
                    await connection.execute(
                        sa.select(s.trading_positions).where(
                            scope_filter(s.trading_positions, self.engine_id, symbol)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                current
                and current["position_id"] == position["position_id"]
                and current["body"].get("structure_prediction_id") == str(ident)
            ):
                monitor_body = {**current["body"], "structure_verified_until": json_value(cursor)}
                if breached:
                    monitor_body["structure_breached_at"] = breached
                await connection.execute(
                    sa.update(s.trading_positions)
                    .where(scope_filter(s.trading_positions, self.engine_id, symbol))
                    .values(body=monitor_body)
                )
                if not breached:
                    return
                await self.predictions._intent(
                    connection,
                    prediction,
                    "CLOSE",
                    f"invalid:{ident}:{current['position_id']}:{current['revision']}",
                    current,
                    now,
                    details={
                        "cause": "STRUCTURE_INVALIDATED",
                        "after_prediction_expiry": now >= prediction.expires_at,
                    },
                )

    async def _stage_changes(self, attempt, symbol, now):
        async with self.jobs.transaction(attempt) as connection:
            stream = (
                (
                    await connection.execute(
                        sa.select(s.prediction_streams).where(
                            scope_filter(s.prediction_streams, self.engine_id, symbol)
                        )
                    )
                )
                .mappings()
                .one()
            )
            checks = stream["body"].get("structure_checks", {})
            heads = (
                (
                    await connection.execute(
                        sa.select(
                            s.prediction_heads,
                            s.prediction_inputs.c.body.label("input"),
                            s.prediction_facts.c.body.label("fact"),
                        )
                        .join(
                            s.prediction_facts,
                            s.prediction_facts.c.id == s.prediction_heads.c.prediction_id,
                        )
                        .join(
                            s.prediction_inputs,
                            s.prediction_inputs.c.id == s.prediction_facts.c.input_id,
                        )
                        .where(
                            scope_filter(
                                s.prediction_heads, self.engine_id, symbol, role="TIMEFRAME"
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
        for head in heads:
            tf = head["timeframe"]
            last = datetime.fromisoformat(
                head["input"]["model_bars"][-1]["close_boundary"].replace("Z", "+00:00")
            )
            close = boundary(now, tf)
            if close <= last or checks.get(tf, {}).get("close") == json_value(close):
                continue
            bars = await self.client.candles(
                boundary(now, "1h"), tf, now + timedelta(seconds=20), inst_id=instrument(symbol)
            )
            if (
                len(bars) != 80
                or any(b.inst_id != instrument(symbol) or b.timeframe != tf for b in bars)
                or bars[-1].close_boundary != close
            ):
                raise DomainError(
                    "STRUCTURE_INPUT_SCOPE", "阶段检查需要同币种周期最新80根已收盘K线", 503
                )
            current = detect_structures(
                bars,
                max(now, self.client.now()),
                tolerance_atr=self.predictions.config.prediction.tolerance_atr,
            )
            signature = sorted((c.kind, c.stage) for c in current)
            original = sorted((c["kind"], c["stage"]) for c in head["input"]["structures"])
            async with self.jobs.transaction(attempt) as connection:
                await self.predictions._lock_symbol(connection, self.engine_id, symbol)
                _, _, updated = await self.jobs.guard(connection, attempt)
                latest = await self.predictions._head(
                    connection, self.engine_id, symbol, tf, "TIMEFRAME"
                )
                if latest["generation"] != head["generation"]:
                    continue
                stream = (
                    (
                        await connection.execute(
                            sa.select(s.prediction_streams).where(
                                scope_filter(s.prediction_streams, self.engine_id, symbol)
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                checks = dict(stream["body"].get("structure_checks", {}))
                checks[tf] = {"close": json_value(close), "signature": signature}
                await connection.execute(
                    sa.update(s.prediction_streams)
                    .where(scope_filter(s.prediction_streams, self.engine_id, symbol))
                    .values(
                        body=json_value({**stream["body"], "structure_checks": checks}),
                        updated_at=updated,
                    )
                )
                paused = await connection.scalar(
                    sa.select(s.runtime_control.c.paused).where(s.runtime_control.c.id == 1)
                )
                if signature != original and not paused:
                    await self.predictions.queue_timeframe(
                        connection,
                        self.engine_id,
                        symbol,
                        tf,
                        f"stage:{head['prediction_id']}:{content_hash(checks[tf])}",
                        updated,
                    )
