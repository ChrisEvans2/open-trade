"""Transactional publication and independent forecast lifecycle.

The model supplies a report, never a database identity, clock, position version,
execution parameter or mutable projection. All those belong to this service.
"""

from __future__ import annotations

from asyncio import sleep
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from trading_quant import CalculationError
from trading_quant.structures import detect_structures

from trading_agent.config import AppConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import INTERVAL_SECONDS, boundary
from trading_agent.jobs.service import Attempt, JobService
from trading_agent.market.features import compute_features, compute_provisional_features
from trading_agent.market.models import Bar, FormingCandle, Observation
from trading_agent.notifications import predictions as notices
from trading_agent.prediction.contracts import (
    ParentDecision,
    PredictionReport,
    PriceLine,
    PublishedPrediction,
)
from trading_agent.prediction.evaluation import EvaluationState, evaluate, scores
from trading_agent.prediction.synthesis import source_input, synthesis_inputs
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock, persist_bars, persist_observations, row_bar


def instrument(symbol):
    if symbol not in {"BTCUSDT", "ETHUSDT"}:
        raise DomainError("SYMBOL_SCOPE", "预测仅支持BTC和ETH")
    return symbol.removesuffix("USDT") + "-USDT-SWAP"


def scope_filter(table, engine_id, symbol, timeframe=None, role=None):
    clauses = [table.c.engine_id == engine_id, table.c.symbol == symbol]
    if timeframe is not None:
        clauses.append(table.c.timeframe == timeframe)
    if role is not None:
        clauses.append(table.c.role == role)
    return sa.and_(*clauses)


def prediction_engine_hash(config, hashes):
    return content_hash(
        {
            "base": config.engine_config_hash,
            "prediction": config.prediction.model_dump(
                exclude={
                    "knowledge_index_path",
                    "execution_enabled",
                    "legacy_config_dir",
                    "account_key",
                }
            ),
            "mode": "structural_prediction_v3",
            "prompts": hashes,
        }
    )


class PredictionService:
    def __init__(self, jobs: JobService, config: AppConfig):
        self.jobs, self.database, self.config = jobs, jobs.database, config

    async def register_engine(self, prompt_hashes):
        if not self.config.hermes.provider or not self.config.hermes.model:
            raise DomainError("MODEL_NOT_CONFIGURED", "预测模型未配置", 503)
        if set(prompt_hashes) != {"parent", "1h", "4h", "6h", "12h", "review"}:
            raise ValueError("Six prediction/review prompt hashes are required")
        digest = prediction_engine_hash(self.config, prompt_hashes)
        async with self.database.transaction() as connection:
            await self.jobs.ownership.guard(connection)
            ident = await connection.scalar(
                insert(s.engines)
                .values(
                    hermes_commit=self.config.hermes.reference_commit,
                    provider=self.config.hermes.provider,
                    model=self.config.hermes.model,
                    prompt_hash=content_hash(prompt_hashes),
                    feature_version=self.config.features.spec_version,
                    config_json=json_value(self.config),
                    engine_hash=digest,
                )
                .on_conflict_do_nothing(index_elements=[s.engines.c.engine_hash])
                .returning(s.engines.c.id)
            )
            return ident or await connection.scalar(
                sa.select(s.engines.c.id).where(s.engines.c.engine_hash == digest)
            )

    def _scope(self, attempt, engine_id):
        role = {"PREDICT_TIMEFRAME": "TIMEFRAME", "PREDICT_PARENT": "PARENT"}.get(attempt.kind)
        symbol, timeframe = attempt.payload.get("symbol"), attempt.payload.get("timeframe", "1h")
        if (
            role is None
            or symbol not in self.config.prediction.symbols
            or timeframe not in self.config.prediction.timeframes
            or attempt.payload.get("engine_id") != str(engine_id)
            or (role == "PARENT" and timeframe != "1h")
        ):
            raise DomainError("PREDICTION_SCOPE", "任务预测范围不匹配", 403)
        return symbol, timeframe, role

    async def _lock_symbol(self, connection, engine_id, symbol):
        await content_lock(connection, f"prediction:{engine_id}:{symbol}")

    async def _head(self, connection, engine_id, symbol, timeframe, role):
        clause = scope_filter(s.prediction_heads, engine_id, symbol, timeframe, role)
        await connection.execute(
            insert(s.prediction_heads)
            .values(
                engine_id=engine_id,
                symbol=symbol,
                timeframe=timeframe,
                role=role,
            )
            .on_conflict_do_nothing()
        )
        return (
            (await connection.execute(sa.select(s.prediction_heads).where(clause))).mappings().one()
        )

    async def _children(self, connection, engine_id, symbol, now):
        rows = (
            (
                await connection.execute(
                    sa.select(
                        s.prediction_heads,
                        s.prediction_facts.c.body.label("fact"),
                        s.prediction_facts.c.expires_at,
                        s.prediction_states.c.body.label("state"),
                        s.prediction_states.c.data_quality,
                    )
                    .join(
                        s.prediction_facts,
                        s.prediction_heads.c.prediction_id == s.prediction_facts.c.id,
                    )
                    .join(
                        s.prediction_states,
                        s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                    )
                    .where(scope_filter(s.prediction_heads, engine_id, symbol, role="TIMEFRAME"))
                    .order_by(s.prediction_heads.c.timeframe)
                )
            )
            .mappings()
            .all()
        )
        available, heads = [], []
        for row in rows:
            eligible = (
                row["state"]["lifecycle"] == "ACTIVE"
                and row["expires_at"] > now
                and row["data_quality"] == "OK"
            )
            heads.append(
                {
                    "timeframe": row["timeframe"],
                    "generation": row["generation"],
                    "prediction_id": row["prediction_id"],
                    "eligible": eligible,
                }
            )
            if eligible:
                available.append(row["fact"]["prediction"])
        position = (
            (
                await connection.execute(
                    sa.select(s.trading_positions).where(
                        scope_filter(s.trading_positions, engine_id, symbol)
                    )
                )
            )
            .mappings()
            .first()
        )
        position = dict(position) if position else None
        decision_position = (
            None
            if position is None
            else {
                **{k: v for k, v in position.items() if k != "updated_at"},
                "body": {
                    k: v
                    for k, v in position["body"].items()
                    if k not in {"structure_verified_until"}
                },
            }
        )
        revision = content_hash({"heads": heads, "position": decision_position})
        return available, position, revision

    async def _rule(self, connection, engine_id, symbol, timeframe, structure, stage, now):
        values = dict(
            engine_id=engine_id,
            symbol=symbol,
            timeframe=timeframe,
            structure=structure,
            stage=stage,
        )
        clause = sa.and_(*(s.horizon_heads.c[k] == v for k, v in values.items()))
        rule_id = await connection.scalar(sa.select(s.horizon_heads.c.rule_id).where(clause))
        if rule_id is None:
            rule_id = uuid4()
            await connection.execute(
                insert(s.horizon_rules).values(
                    id=rule_id,
                    **values,
                    bars=self.config.prediction.initial_horizon_bars,
                    reason="INITIAL_UNLEARNED",
                    available_at=now,
                    body={"evidence_status": "UNVALIDATED", "sample_count": 0},
                )
            )
            await connection.execute(insert(s.horizon_heads).values(**values, rule_id=rule_id))
        row = (
            (
                await connection.execute(
                    sa.select(s.horizon_rules).where(s.horizon_rules.c.id == rule_id)
                )
            )
            .mappings()
            .one()
        )
        return {
            "id": row["id"],
            "bars": row["bars"],
            "structure": structure,
            "stage": stage,
            "reason": row["reason"],
        }

    async def freeze(
        self,
        attempt: Attempt,
        engine_id: UUID,
        bars: tuple[Bar, ...],
        observations: tuple[Observation, ...] = (),
        *,
        forming_candle: FormingCandle | None = None,
        forming_candle_missing_reason: str = "NOT_COLLECTED",
        benchmark_bars: tuple[Bar, ...] = (),
        benchmark_missing_reason: str | None = "NOT_COLLECTED",
        liquidation_sampler=None,
        instrument_metadata=None,
    ):
        symbol, timeframe, role = self._scope(attempt, engine_id)
        if role == "PARENT":
            return await self.freeze_parent(attempt, engine_id)
        async with self.jobs.transaction(attempt) as connection:
            await self._lock_symbol(connection, engine_id, symbol)
            _, _, now = await self.jobs.guard(connection, attempt)
            existing = (
                (
                    await connection.execute(
                        sa.select(s.prediction_inputs).where(
                            s.prediction_inputs.c.job_id == attempt.job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["engine_id"] != engine_id:
                    raise DomainError("ENGINE_MISMATCH", "不能替换已冻结的输入", 409)
                return existing["id"]
            if (
                len(bars) != 80
                or any(b.inst_id != instrument(symbol) or b.timeframe != timeframe for b in bars)
                or bars[-1].close_boundary != boundary(now, timeframe)
            ):
                raise DomainError("INPUT_SCOPE", "需要该标的周期最新80根已收盘K线")
            if forming_candle is not None:
                forming_candle = FormingCandle.model_validate(forming_candle)
                if (
                    forming_candle.inst_id != instrument(symbol)
                    or forming_candle.timeframe != timeframe
                    or forming_candle.open_at != bars[-1].close_boundary
                    or forming_candle.available_at > now
                    or (now - forming_candle.requested_at).total_seconds()
                    > self.config.prediction.context_max_age_seconds
                ):
                    raise DomainError("FORMING_CANDLE_SCOPE", "未收盘快照标的、周期或可用时点无效")
            try:
                candidates = tuple(
                    asdict(c)
                    for c in detect_structures(
                        bars, now, tolerance_atr=self.config.prediction.tolerance_atr
                    )
                )
            except CalculationError as exc:
                raise DomainError(exc.code, exc.message) from exc
            if any(o.inst_id != instrument(symbol) or o.available_at > now for o in observations):
                raise DomainError("OBSERVATION_SCOPE", "外部指标包含其他标的或未来数据")
            if self.config.market.taker_source == "trade_minutes":
                from trading_agent.storage.trades import trade_windows

                # Read only durable, decision-time revisions in the same guarded
                # freeze transaction. No network fetch or legacy source fallback.
                observations = (
                    *(o for o in observations if o.kind != "taker_volume"),
                    *await trade_windows(
                        connection,
                        instrument(symbol),
                        timeframe,
                        bars[-1].close_boundary,
                        now,
                    ),
                )
            if benchmark_bars:
                from trading_agent.market.snapshots import validate_bars

                if symbol != "ETHUSDT" or any(b.inst_id != "BTC-USDT-SWAP" for b in benchmark_bars):
                    raise DomainError("BENCHMARK_SCOPE", "ETH相对指标仅接受独立BTC合约基准")
                validate_bars(benchmark_bars, boundary(now, "1h"), timeframe, now)
                benchmark_bars = await persist_bars(connection, benchmark_bars, "BTCUSDT")
            if liquidation_sampler is not None:
                observations = (
                    *observations,
                    *await liquidation_sampler.windows(
                        connection,
                        bars[-1].close_boundary,
                        now,
                        instrument=instrument_metadata,
                        inst_id=instrument(symbol),
                        timeframes=(timeframe,),
                    ),
                )
            observations = await persist_observations(connection, observations)
            features = compute_features(
                bars,
                self.config.features,
                observations,
                decision_at=now,
                benchmark_bars=benchmark_bars,
            )
            from trading_agent.market.context import funding_context, indicator_policy

            missing = [
                f.name
                for f in features
                if f.name in self.config.features.required_features and f.quality != "OK"
            ]
            if missing:
                raise DomainError("REQUIRED_FEATURE_MISSING", "必要指标缺失: " + ",".join(missing))
            provisional_features = (
                compute_provisional_features(bars, forming_candle, self.config.features, now)
                if forming_candle is not None
                else ()
            )
            stored = await persist_bars(connection, bars, symbol)
            rules = []
            for structure, stage in sorted(
                {("UNKNOWN", "UNKNOWN"), *((c["kind"], c["stage"]) for c in candidates)}
            ):
                rules.append(
                    await self._rule(
                        connection, engine_id, symbol, timeframe, structure, stage, now
                    )
                )
            head = await self._head(connection, engine_id, symbol, timeframe, role)
            children, position, revision = await self._children(connection, engine_id, symbol, now)
            value = json_value(
                {
                    "schema_version": 3,
                    "candle_context_version": 1,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "role": role,
                    "inst_id": instrument(symbol),
                    "decision_at": now,
                    "model_bars": stored[-30:],
                    "forming_candle": forming_candle,
                    "forming_candle_quality": {
                        "quality": "OK" if forming_candle is not None else "MISSING",
                        "missing_reason": None
                        if forming_candle is not None
                        else forming_candle_missing_reason,
                    },
                    "bar_ids": [b.id for b in stored],
                    "observation_ids": [o.id for o in observations],
                    "features": features,
                    "indicator_policy": indicator_policy(self.config.features.market_basket),
                    "funding_context": funding_context(observations, now),
                    "external_data_diagnostics": [
                        {
                            "source": o.source,
                            "kind": o.kind,
                            "quality": o.quality,
                            "reason": o.payload.get("reason") or o.payload.get("missing_reason"),
                            "timeframe": o.payload.get("timeframe"),
                            "observation_id": o.id,
                        }
                        for o in observations
                        if o.quality != "OK"
                    ],
                    "btc_benchmark": {
                        "inst_id": "BTC-USDT-SWAP",
                        "timeframe": timeframe,
                        "quality": "OK"
                        if benchmark_bars
                        else "NOT_APPLICABLE"
                        if symbol == "BTCUSDT"
                        else "MISSING",
                        "missing_reason": None if benchmark_bars else benchmark_missing_reason,
                        "evidence_strength": "WEAK",
                        "bar_ids": [b.id for b in benchmark_bars],
                        "calculation_bars": benchmark_bars[-21:],
                    },
                    "provisional_features": provisional_features,
                    "provisional_feature_basis": {
                        "status": "PROVISIONAL",
                        "closed_bars": 80,
                        "forming_bars": 1 if forming_candle is not None else 0,
                        "as_of": forming_candle.available_at
                        if forming_candle is not None
                        else None,
                        "scope": "OHLCV_ONLY",
                        "volume_time_normalized": False,
                    },
                    "structures": candidates,
                    "tolerance_atr": self.config.prediction.tolerance_atr,
                    "fallback_tolerance": Decimal(
                        next(f.value for f in features if f.name == "atr_pct")
                    )
                    * stored[-1].close
                    * self.config.prediction.tolerance_atr,
                    "horizon_rules": rules,
                    "reference_price": stored[-1].close,
                    "children": children if role == "PARENT" else [],
                    "missing_timeframes": [
                        tf
                        for tf in self.config.prediction.timeframes
                        if tf not in {c["report"]["timeframe"] for c in children}
                    ]
                    if role == "PARENT"
                    else [],
                    "position": position if position and position["status"] == "OPEN" else None,
                    "context_revision": revision,
                    "head_generation": head["generation"],
                    "supersedes_id": head["prediction_id"],
                    "fallback_lines": [
                        dict(at=stored[-1].close_boundary, price=price, slope_per_second=0)
                        for price in sorted(
                            {
                                stored[-1].close,
                                max(b.high for b in stored),
                                min(b.low for b in stored),
                            }
                        )
                    ],
                }
            )
            ident = uuid4()
            await self.jobs.guard(connection, attempt)
            await connection.execute(
                insert(s.prediction_inputs).values(
                    id=ident,
                    job_id=attempt.job_id,
                    engine_id=engine_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    role=role,
                    decision_at=now,
                    body=value,
                    content_hash=content_hash(value),
                )
            )
            return ident

    async def freeze_parent(self, attempt, engine_id):
        symbol, timeframe, role = self._scope(attempt, engine_id)
        if role != "PARENT":
            raise DomainError("PREDICTION_SCOPE", "只有父任务可以冻结综合输入", 403)
        async with self.jobs.transaction(attempt) as connection:
            await self._lock_symbol(connection, engine_id, symbol)
            _, _, now = await self.jobs.guard(connection, attempt)
            existing = (
                (
                    await connection.execute(
                        sa.select(s.prediction_inputs).where(
                            s.prediction_inputs.c.job_id == attempt.job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["engine_id"] != engine_id:
                    raise DomainError("ENGINE_MISMATCH", "不能替换已冻结的输入", 409)
                return existing["id"]
            children, position, revision = await self._children(connection, engine_id, symbol, now)
            evidence = await synthesis_inputs(connection, children, engine_id, symbol)
            rules = []
            for structure, stage in sorted(
                {("UNKNOWN", "UNKNOWN"), *((c["kind"], c["stage"]) for c in evidence["structures"])}
            ):
                rules.append(
                    await self._rule(connection, engine_id, symbol, "1h", structure, stage, now)
                )
            head = await self._head(connection, engine_id, symbol, timeframe, role)
            value = json_value(
                {
                    **evidence,
                    "schema_version": 4,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "role": role,
                    "inst_id": instrument(symbol),
                    "decision_at": now,
                    "horizon_rules": rules,
                    "children": children,
                    "missing_timeframes": [
                        tf
                        for tf in self.config.prediction.timeframes
                        if tf not in {c["report"]["timeframe"] for c in children}
                    ],
                    "position": position if position and position["status"] == "OPEN" else None,
                    "context_revision": revision,
                    "head_generation": head["generation"],
                    "supersedes_id": head["prediction_id"],
                }
            )
            ident = uuid4()
            await self.jobs.guard(connection, attempt)
            await connection.execute(
                insert(s.prediction_inputs).values(
                    id=ident,
                    job_id=attempt.job_id,
                    engine_id=engine_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    role=role,
                    decision_at=now,
                    body=value,
                    content_hash=content_hash(value),
                )
            )
            return ident

    async def child_evidence(self, attempt, child_ref):
        from trading_agent.prediction.submissions import model_context

        async with self.jobs.transaction(attempt) as connection:
            parent = await self._input(connection, attempt)
            frozen = parent["body"]
            if (
                frozen.get("input_kind") != "CHILD_SYNTHESIS"
                or child_ref not in frozen["child_sources"]
            ):
                raise DomainError("CHILD_EVIDENCE_SCOPE", "只能读取本次父综合引用的子预测证据", 403)
            child = next(
                c for c in frozen["children"] if f"child.{c['report']['timeframe']}" == child_ref
            )
            row = await source_input(
                connection,
                child,
                parent["engine_id"],
                parent["symbol"],
                frozen["child_sources"][child_ref]["content_hash"],
            )
            return {
                "source_child_ref": child_ref,
                "purpose": "FROZEN_CHILD_EVIDENCE",
                "published_at": child["published_at"],
                "expires_at": child["expires_at"],
                "context": model_context({**row["body"], "input_id": str(row["id"])}),
            }

    async def _input(self, connection, attempt):
        row = (
            (
                await connection.execute(
                    sa.select(s.prediction_inputs).where(
                        s.prediction_inputs.c.job_id == attempt.job_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("INPUT_NOT_FROZEN", "预测输入尚未冻结", 409)
        self._scope(attempt, row["engine_id"])
        return row

    async def context(self, attempt):
        async with self.jobs.transaction(attempt) as connection:
            row = await self._input(connection, attempt)
            recalls = (
                (
                    await connection.execute(
                        sa.select(s.knowledge_recalls.c.body)
                        .where(s.knowledge_recalls.c.input_id == row["id"])
                        .order_by(s.knowledge_recalls.c.created_at)
                    )
                )
                .scalars()
                .all()
            )
            frozen = row["body"]
            conditions = [
                {
                    "structure": "UNKNOWN",
                    "structure_id": None,
                    "stage": "UNKNOWN",
                    "lines": frozen["fallback_lines"],
                    "tolerance": frozen["fallback_tolerance"],
                },
                *(
                    {
                        "structure": candidate["kind"],
                        "structure_id": candidate["id"],
                        "stage": candidate["stage"],
                        "lines": [candidate["upper"], candidate["lower"]]
                        + [
                            dict(at=p["at"], price=p["price"], slope_per_second=0)
                            for p in candidate["pivots"]
                        ],
                        "tolerance": candidate["tolerance"],
                    }
                    for candidate in frozen["structures"]
                ),
            ]
            if frozen.get("input_kind") == "CHILD_SYNTHESIS":
                conditions = frozen["boundary_options"]
            position = frozen["position"]
            references = {
                "evidence_ids": [str(row["id"])],
                "condition_references": conditions,
                "horizon_rules": frozen["horizon_rules"],
            }
            if frozen["role"] == "PARENT":
                references.update(
                    child_prediction_ids=[child["id"] for child in frozen["children"]],
                    context_revision=frozen["context_revision"],
                    position_id=position["position_id"]
                    if position and position["status"] == "OPEN"
                    else None,
                )
            return {
                "input_id": str(row["id"]),
                **{
                    k: v
                    for k, v in frozen.items()
                    if frozen.get("input_kind") != "CHILD_SYNTHESIS"
                    or k not in {"reference_price", "reference_price_source", "execution_basis"}
                },
                "knowledge_recalls": list(recalls),
                "submission_references": references,
            }

    async def _validate_report(self, connection, row, report):
        frozen = row["body"]
        if report.symbol != row["symbol"] or report.timeframe != row["timeframe"]:
            raise DomainError("REPORT_SCOPE", "报告与任务币种周期不一致")
        rules = [r for r in frozen["horizon_rules"] if r["id"] == str(report.horizon_rule_id)]
        if len(rules) != 1 or any(
            rules[0][k] != getattr(report, name)
            for k, name in (
                ("bars", "horizon_bars"),
                ("structure", "structure"),
                ("stage", "stage"),
            )
        ):
            raise DomainError("HORIZON_RULE", "期限必须引用冻结的同结构阶段规则")
        candidate = next((c for c in frozen["structures"] if c["id"] == report.structure_id), None)
        if report.structure != "UNKNOWN" and (
            candidate is None
            or candidate["kind"] != report.structure
            or candidate["stage"] != report.stage
        ):
            raise DomainError("STRUCTURE_REFERENCE", "形态与阶段必须引用程序候选")
        if frozen.get("input_kind") == "CHILD_SYNTHESIS":
            lines, tolerance = [], Decimal("0")
        elif candidate:
            lines = [candidate["upper"], candidate["lower"]] + [
                dict(at=p["at"], price=p["price"], slope_per_second=0) for p in candidate["pivots"]
            ]
            tolerance = Decimal(candidate["tolerance"])
        else:
            lines = frozen["fallback_lines"]
            atr_pct = next(f["value"] for f in frozen["features"] if f["name"] == "atr_pct")
            tolerance = (
                Decimal(atr_pct)
                * Decimal(frozen["reference_price"])
                * Decimal(frozen["tolerance_atr"])
            )
        for scenario in report.scenarios:
            for condition in (
                scenario.trigger,
                scenario.invalidation,
                *scenario.boundary_expectations,
            ):
                if frozen.get("input_kind") == "CHILD_SYNTHESIS":
                    permitted = any(
                        condition.tolerance == Decimal(option["tolerance"])
                        and abs(
                            condition.line.value_at(row["decision_at"])
                            - PriceLine.model_validate(option["line"]).value_at(row["decision_at"])
                        )
                        <= Decimal("0.000000000001")
                        and condition.line.slope_per_second
                        == PriceLine.model_validate(option["line"]).slope_per_second
                        for option in frozen["boundary_options"]
                    )
                    if not permitted:
                        raise DomainError(
                            "BOUNDARY_REFERENCE", "父边界和容差必须引用冻结子报告证据"
                        )
                    continue
                if condition.tolerance != tolerance or not any(
                    abs(
                        condition.line.value_at(row["decision_at"])
                        - PriceLine.model_validate(line).value_at(row["decision_at"])
                    )
                    <= Decimal("0.000000000001")
                    and condition.line.slope_per_second
                    == PriceLine.model_validate(line).slope_per_second
                    for line in lines
                ):
                    raise DomainError("BOUNDARY_REFERENCE", "边界和容差必须引用冻结的结构证据")
        if not set(map(str, report.evidence_ids)) <= {
            str(row["id"]),
            *frozen["bar_ids"],
            *frozen.get("observation_ids", []),
            *(source["input_id"] for source in frozen.get("child_sources", {}).values()),
        }:
            raise DomainError("EVIDENCE_REFERENCE", "证据必须来自本次冻结输入")
        recalls = (
            (
                await connection.execute(
                    sa.select(s.knowledge_recalls.c.body).where(
                        s.knowledge_recalls.c.input_id == row["id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        allowed = {card["id"] for recall in recalls for card in recall["items"]}
        if not set(map(str, report.knowledge_version_ids)) <= allowed:
            raise DomainError("KNOWLEDGE_REFERENCE", "知识引用必须经过本任务检索并冻结")

    async def _await_child_verification(self, attempt):
        # A minute candle may be pending for a few seconds while the model
        # submits. Wait outside transactions, then retain every publish guard.
        for check in range(5):
            async with self.jobs.transaction(attempt) as connection:
                row = await self._input(connection, attempt)
                _, _, now = await self.jobs.guard(connection, attempt)
                children = [UUID(child["id"]) for child in row["body"]["children"]]
                pending = await connection.scalar(
                    sa.select(
                        sa.exists(
                            sa.select(1)
                            .select_from(s.prediction_heads)
                            .join(
                                s.prediction_facts,
                                s.prediction_facts.c.id == s.prediction_heads.c.prediction_id,
                            )
                            .join(
                                s.prediction_states,
                                s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                            )
                            .where(
                                s.prediction_heads.c.prediction_id.in_(children),
                                s.prediction_states.c.data_quality != "OK",
                                s.prediction_states.c.body["lifecycle"].as_string() == "ACTIVE",
                                s.prediction_facts.c.expires_at > now,
                            )
                        )
                    )
                )
            if not pending:
                return
            if check < 4:
                await sleep(1)
        raise DomainError("MINUTE_DATA_PENDING", "分钟核验尚未完成，暂不接受综合提交", 503)

    async def publish(
        self, attempt: Attempt, report: PredictionReport | ParentDecision, *, on_published=None
    ):
        decision = report if isinstance(report, ParentDecision) else None
        report = PredictionReport.model_validate(decision.prediction if decision else report)
        if (attempt.kind == "PREDICT_PARENT") != (decision is not None):
            raise DomainError("REPORT_ROLE", "父预测必须提交综合决策，周期预测不能提交决策")
        if decision is not None:
            await self._await_child_verification(attempt)
        async with self.jobs.transaction(attempt) as connection:
            row = await self._input(connection, attempt)
            await self._lock_symbol(connection, row["engine_id"], row["symbol"])
            _, _, now = await self.jobs.guard(connection, attempt)
            if now > row["decision_at"] + timedelta(
                seconds=self.config.prediction.context_max_age_seconds
            ):
                raise DomainError("CONTEXT_EXPIRED", "冻结行情已过时，需要重新分析", 409)
            previous = (
                (
                    await connection.execute(
                        sa.select(s.prediction_facts).where(
                            s.prediction_facts.c.job_id == attempt.job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            submission = json_value(decision or report)
            if previous:
                if previous["body"]["submission"] != submission:
                    raise DomainError("PUBLICATION_CONFLICT", "已发布内容不能更改", 409)
                ident = UUID(previous["body"]["prediction"]["id"])
                if on_published:
                    await on_published(connection, ident)
                return ident
            await self._validate_report(connection, row, report)
            head = await self._head(
                connection, row["engine_id"], row["symbol"], row["timeframe"], row["role"]
            )
            if head["generation"] != row["body"]["head_generation"]:
                raise DomainError("CONTEXT_CHANGED", "该周期已有更新结果", 409)
            children, position, revision = await self._children(
                connection, row["engine_id"], row["symbol"], now
            )
            if decision:
                if (
                    decision.context_revision != revision
                    or row["body"]["context_revision"] != revision
                ):
                    raise DomainError("CONTEXT_CHANGED", "子预测或持仓已经变化，需要重新综合", 409)
                child_ids = {str(c["id"]) for c in children}
                if set(map(str, decision.child_prediction_ids)) != child_ids:
                    raise DomainError("CHILD_REFERENCE", "必须综合全部仍有效周期，并注明缺失周期")
                current_position = (
                    position["position_id"] if position and position["status"] == "OPEN" else None
                )
                if decision.position_id != current_position:
                    raise DomainError("POSITION_CHANGED", "决策必须绑定当前持仓或明确空仓", 409)
                if decision.action.startswith("OPEN") and not children:
                    raise DomainError("MISSING_CONTEXT", "没有有效周期预测时不能开仓", 409)
            start = boundary(now, "1m")
            if start < now:
                start += timedelta(minutes=1)
            try:
                prediction = PublishedPrediction(
                    id=uuid4(),
                    engine_id=row["engine_id"],
                    snapshot_id=row["id"],
                    inst_id=instrument(row["symbol"]),
                    role=row["role"],
                    published_at=now,
                    expires_at=now
                    + timedelta(seconds=INTERVAL_SECONDS[report.timeframe] * report.horizon_bars),
                    observation_start=start,
                    reference_price=row["body"]["reference_price"],
                    report=report,
                    supersedes_id=head["prediction_id"],
                )
            except ValidationError:
                # Report schemas cannot validate geometry at the actual publish
                # time. Reject model geometry as a domain error, never a 500.
                raise DomainError(
                    "PREDICTION_GEOMETRY",
                    "上涨目标下沿须高于触发价，下跌目标上沿须低于触发价；期限内边界须为正",
                    422,
                ) from None
            value = json_value({"prediction": prediction, "submission": submission})
            await connection.execute(
                insert(s.prediction_facts).values(
                    id=prediction.id,
                    job_id=attempt.job_id,
                    attempt_id=attempt.id,
                    input_id=row["id"],
                    engine_id=row["engine_id"],
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    role=row["role"],
                    published_at=now,
                    expires_at=prediction.expires_at,
                    body=value,
                    content_hash=content_hash(value),
                )
            )
            initial = evaluate(prediction, (), now)
            await connection.execute(
                insert(s.prediction_states).values(
                    prediction_id=prediction.id,
                    revision=1,
                    data_quality=initial.data_quality,
                    body=json_value(initial.state),
                    checked_at=now,
                )
            )
            await connection.execute(
                sa.update(s.prediction_heads)
                .where(
                    scope_filter(
                        s.prediction_heads,
                        row["engine_id"],
                        row["symbol"],
                        row["timeframe"],
                        row["role"],
                    )
                )
                .values(
                    prediction_id=prediction.id, generation=head["generation"] + 1, updated_at=now
                )
            )
            if decision:
                decision_id = uuid4()
                await connection.execute(
                    insert(s.prediction_decisions).values(
                        id=decision_id,
                        prediction_id=prediction.id,
                        attempt_id=attempt.id,
                        body=submission,
                        created_at=now,
                    )
                )
                await self._decision_intent(
                    connection, prediction, decision, decision_id, position, now
                )
            elif not attempt.payload.get("analysis_managed"):
                await self.queue_parent(connection, row["engine_id"], row["symbol"], now)
            await self.jobs.guard(connection, attempt)
            if on_published:
                await on_published(connection, prediction.id)
            if decision:
                await notices.published(connection, self.config, prediction, decision, now)
            return prediction.id

    async def queue_parent(self, connection, engine_id, symbol, now, cause=None):
        if rounds := getattr(self, "rounds", None):
            if cause is None:
                _, _, revision = await self._children(connection, engine_id, symbol, now)
                cause = f"parent:{revision}"
            return await rounds.event(connection, engine_id, symbol, (), "UPDATE", cause, now)
        await self._lock_symbol(connection, engine_id, symbol)
        # Coalesce only jobs whose input is not frozen. An in-flight synthesis can
        # become stale and must leave room for a new job with the latest context.
        pending = await connection.scalar(
            sa.select(s.jobs.c.id)
            .where(
                s.jobs.c.kind == "PREDICT_PARENT",
                s.jobs.c.state.in_(("QUEUED", "RETRY_WAIT", "AWAITING_DATA")),
                s.jobs.c.payload["engine_id"].as_string() == str(engine_id),
                s.jobs.c.payload["symbol"].as_string() == symbol,
                ~sa.exists(sa.select(1).where(s.prediction_inputs.c.job_id == s.jobs.c.id)),
            )
            .limit(1)
        )
        if pending:
            return pending
        _, _, revision = await self._children(connection, engine_id, symbol, now)
        head = await self._head(connection, engine_id, symbol, "1h", "PARENT")
        revision = content_hash(
            {
                "children": revision,
                "parent_generation": head["generation"],
                "retry_minute": boundary(now, "1m"),
            }
        )
        return await self.jobs.create(
            connection,
            "PREDICT_PARENT",
            f"parent:{engine_id}:{symbol}:{revision}",
            {"engine_id": str(engine_id), "symbol": symbol, "timeframe": "1h"},
            now,
        )

    async def queue_retry(self, connection, attempt, now, *, delay_seconds, recovery=False):
        """A failed analysis keeps its immutable input; its successor freezes new data."""
        engine_id = UUID(attempt.payload["engine_id"])
        symbol = attempt.payload["symbol"]
        timeframe = attempt.payload.get("timeframe", "1h")
        await self._lock_symbol(connection, engine_id, symbol)
        pending = await connection.scalar(
            sa.select(s.jobs.c.id)
            .where(
                s.jobs.c.kind == attempt.kind,
                s.jobs.c.state.in_(("QUEUED", "RETRY_WAIT", "AWAITING_DATA")),
                s.jobs.c.payload["engine_id"].as_string() == str(engine_id),
                s.jobs.c.payload["symbol"].as_string() == symbol,
                s.jobs.c.payload["timeframe"].as_string() == timeframe,
                ~sa.exists(sa.select(1).where(s.prediction_inputs.c.job_id == s.jobs.c.id)),
            )
            .limit(1)
        )
        if pending:
            return pending
        return await self.jobs.create(
            connection,
            attempt.kind,
            f"prediction-retry:{attempt.id}",
            {
                "engine_id": str(engine_id),
                "symbol": symbol,
                "timeframe": timeframe,
                "analysis_retry_of": str(attempt.job_id),
                "analysis_retry_count": 0
                if recovery
                else attempt.payload.get("analysis_retry_count", 0) + 1,
            },
            now + timedelta(seconds=delay_seconds),
            deadline_at=attempt.deadline_at,
        )

    async def queue_timeframe(self, connection, engine_id, symbol, timeframe, cause, now):
        if rounds := getattr(self, "rounds", None):
            return await rounds.event(
                connection, engine_id, symbol, (timeframe,), "UPDATE", cause, now
            )
        await self._lock_symbol(connection, engine_id, symbol)
        pending = await connection.scalar(
            sa.select(s.jobs.c.id)
            .where(
                s.jobs.c.kind == "PREDICT_TIMEFRAME",
                s.jobs.c.state.in_(("QUEUED", "RETRY_WAIT", "AWAITING_DATA")),
                s.jobs.c.payload["engine_id"].as_string() == str(engine_id),
                s.jobs.c.payload["symbol"].as_string() == symbol,
                s.jobs.c.payload["timeframe"].as_string() == timeframe,
                ~sa.exists(sa.select(1).where(s.prediction_inputs.c.job_id == s.jobs.c.id)),
            )
            .limit(1)
        )
        if pending:
            return pending
        return await self.jobs.create(
            connection,
            "PREDICT_TIMEFRAME",
            f"timeframe:{engine_id}:{symbol}:{timeframe}:{cause}",
            {"engine_id": str(engine_id), "symbol": symbol, "timeframe": timeframe},
            now,
        )

    async def _intent(
        self, connection, prediction, action, cause, position, now, decision_id=None, details=None
    ):
        ident = uuid4()
        position_id = position["position_id"] if position and position["status"] == "OPEN" else None
        if action == "CLOSE" and position_id is None:
            return None
        inserted = await connection.scalar(
            insert(s.trade_intents)
            .values(
                id=ident,
                engine_id=prediction.engine_id,
                symbol=prediction.report.symbol,
                prediction_id=prediction.id,
                decision_id=decision_id,
                cause_key=cause,
                action=action,
                position_id=position_id,
                position_version=position["revision"] if position_id else None,
                body=json_value(
                    {
                        "forecast_target": [s.target for s in prediction.report.scenarios],
                        "details": details or {},
                        "expires_at": prediction.expires_at,
                    }
                ),
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[s.trade_intents.c.cause_key])
            .returning(s.trade_intents.c.id)
        )
        if inserted is None:
            return await connection.scalar(
                sa.select(s.trade_intents.c.id).where(s.trade_intents.c.cause_key == cause)
            )
        await connection.execute(
            insert(s.trade_intent_states).values(
                intent_id=ident, status="PENDING", body={}, updated_at=now
            )
        )
        await self.jobs.create(
            connection, "PREDICT_EXECUTE", f"execute:{ident}", {"intent_id": str(ident)}, now
        )
        return ident

    async def _decision_intent(self, connection, prediction, decision, decision_id, position, now):
        opened = position and position["status"] == "OPEN"
        direction = decision.selected_direction or prediction.report.primary_direction
        reverse = opened and direction is not None and direction != position["body"]["direction"]
        action = "CLOSE" if reverse else decision.action
        if action == "HOLD" or (opened and action.startswith("OPEN")):
            # Same-direction synthesis takes over structure monitoring, without resizing.
            if opened and direction == position["body"]["direction"]:
                await connection.execute(
                    sa.update(s.trading_positions)
                    .where(
                        scope_filter(
                            s.trading_positions, prediction.engine_id, prediction.report.symbol
                        )
                    )
                    .values(
                        body={
                            **{
                                k: v
                                for k, v in position["body"].items()
                                if k != "structure_breached_at"
                            },
                            "structure_prediction_id": str(prediction.id),
                            "structure_verified_until": json_value(prediction.observation_start),
                        },
                        updated_at=now,
                    )
                )
            return
        await self._intent(
            connection,
            prediction,
            action,
            f"decision:{decision_id}",
            position,
            now,
            decision_id,
            {"trade_style": decision.trade_style, "reversal": bool(reverse)},
        )

    async def verify(
        self, attempt: Attempt, prediction_id: UUID, bars: tuple[Bar, ...], *, coordinator=False
    ):
        trusted = (
            coordinator
            and attempt.kind == "PREDICT_PARENT"
            and attempt.payload.get("analysis_round")
            and not attempt.payload.get("analysis_managed")
        )
        if attempt.kind != "PREDICT_VERIFY" and not trusted:
            raise DomainError("FORBIDDEN", "只有确定性验证任务可以写验证事实", 403)
        async with self.jobs.transaction(attempt) as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(s.prediction_facts).where(
                            s.prediction_facts.c.id == prediction_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            if (
                attempt.payload.get("engine_id") != str(row["engine_id"])
                or attempt.payload.get("symbol") != row["symbol"]
            ):
                raise DomainError("PREDICTION_SCOPE", "验证任务范围不一致", 403)
            await self._lock_symbol(connection, row["engine_id"], row["symbol"])
            _, _, now = await self.jobs.guard(connection, attempt)
            prediction = PublishedPrediction.model_validate(row["body"]["prediction"])
            state_row = (
                (
                    await connection.execute(
                        sa.select(s.prediction_states).where(
                            s.prediction_states.c.prediction_id == prediction_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            state = EvaluationState.model_validate(state_row["body"])
            # Persist only validated scope; the evaluator rejects mixed/future/unsorted input.
            evaluate(prediction, bars, now, state)
            stored = await persist_bars(connection, bars, row["symbol"])
            result = evaluate(prediction, stored, now, state)
            if (
                json_value(result.state) == state_row["body"]
                and result.data_quality == state_row["data_quality"]
            ):
                return result
            revision = state_row["revision"] + 1
            await connection.execute(
                insert(s.prediction_evaluations).values(
                    prediction_id=prediction_id,
                    attempt_id=attempt.id,
                    revision=revision,
                    evaluated_at=now,
                    body=json_value(
                        {
                            "result": result,
                            "scores": scores(prediction, result.state),
                            "bar_ids": [b.id for b in stored],
                        }
                    ),
                )
            )
            await connection.execute(
                sa.update(s.prediction_states)
                .where(s.prediction_states.c.prediction_id == prediction_id)
                .values(
                    revision=revision,
                    body=json_value(result.state),
                    data_quality=result.data_quality,
                    checked_at=now,
                )
            )
            head = await self._head(
                connection, row["engine_id"], row["symbol"], row["timeframe"], row["role"]
            )
            is_current = head["prediction_id"] == prediction_id
            ended = state.lifecycle == "ACTIVE" and result.state.lifecycle != "ACTIVE"
            if is_current and ended:
                await connection.execute(
                    sa.update(s.prediction_heads)
                    .where(
                        scope_filter(
                            s.prediction_heads,
                            row["engine_id"],
                            row["symbol"],
                            row["timeframe"],
                            row["role"],
                        )
                    )
                    .values(generation=head["generation"] + 1, updated_at=now)
                )
                if row["role"] == "TIMEFRAME":
                    await self.queue_timeframe(
                        connection,
                        row["engine_id"],
                        row["symbol"],
                        row["timeframe"],
                        f"ended:{prediction_id}",
                        now,
                    )
            # Transient candle gaps are handled deterministically. Recovery is
            # checked once after the market batch, when all child qualities have
            # been updated, so minute-boundary gaps cannot starve other roles.
            if is_current and ended:
                if rounds := getattr(self, "rounds", None):
                    await rounds.event(
                        connection,
                        row["engine_id"],
                        row["symbol"],
                        () if row["role"] == "PARENT" else (row["timeframe"],),
                        "PARENT_ENDED" if row["role"] == "PARENT" else "UPDATE",
                        f"ended:{prediction_id}",
                        now,
                    )
                else:
                    await self.queue_parent(connection, row["engine_id"], row["symbol"], now)
            if row["role"] == "PARENT" and result.state.lifecycle == "INVALIDATED":
                position = (
                    (
                        await connection.execute(
                            sa.select(s.trading_positions).where(
                                scope_filter(s.trading_positions, row["engine_id"], row["symbol"])
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if position and position["body"].get("structure_prediction_id") == str(
                    prediction_id
                ):
                    await self._intent(
                        connection,
                        prediction,
                        "CLOSE",
                        f"invalid:{prediction_id}:{position['position_id']}:{position['revision']}",
                        position,
                        now,
                    )
            await self.jobs.guard(connection, attempt)
            return result

    async def minute_bars(self, inst_id, start, end, as_of):
        """Latest revisions known by as_of; gap detection belongs to evaluator."""
        async with self.database.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(s.market_bars)
                        .where(
                            s.market_bars.c.inst_id == inst_id,
                            s.market_bars.c.interval == "1m",
                            s.market_bars.c.open_at >= start,
                            s.market_bars.c.close_boundary <= end,
                            s.market_bars.c.available_at <= as_of,
                        )
                        .distinct(s.market_bars.c.open_at)
                        .order_by(s.market_bars.c.open_at, s.market_bars.c.revision_no.desc())
                    )
                )
                .mappings()
                .all()
            )
            return tuple(row_bar(r) for r in rows)
