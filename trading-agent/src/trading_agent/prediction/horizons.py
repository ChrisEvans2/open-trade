"""Paired counterfactual horizon tests; never edits a published prediction."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.config import PredictionConfig, StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import INTERVAL_SECONDS
from trading_agent.prediction.contracts import HorizonBars, PublishedPrediction
from trading_agent.prediction.evaluation import evaluate
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock


class HorizonResult(StrictModel):
    bars: HorizonBars
    outcome: str
    triggered: bool
    elapsed_bars: Decimal
    utility: Decimal
    complete: bool


class PairedResult(StrictModel):
    prediction_id: UUID
    incumbent: HorizonResult
    candidate: HorizonResult
    data_quality: str
    bar_ids: tuple[UUID, ...]

    @property
    def valid(self):
        return (
            self.data_quality == "OK"
            and self.incumbent.complete
            and self.candidate.complete
            and "AMBIGUOUS" not in {self.incumbent.outcome, self.candidate.outcome}
        )


def counterfactual(prediction, bars_count, bars, as_of, config):
    report = prediction.report.model_copy(update={"horizon_bars": bars_count})
    changed = PublishedPrediction.model_validate(
        {
            **prediction.model_dump(),
            "report": report,
            "expires_at": prediction.published_at
            + timedelta(seconds=INTERVAL_SECONDS[report.timeframe] * bars_count),
        }
    )
    result = evaluate(changed, bars, as_of)
    state = result.state
    ended = state.ended_at or min(as_of, changed.expires_at)
    elapsed = (
        Decimal(str((ended - prediction.published_at).total_seconds()))
        / INTERVAL_SECONDS[report.timeframe]
    )
    reward = {"ACHIEVED": Decimal(1), "INVALIDATED": Decimal(-1)}.get(state.lifecycle, Decimal(0))
    direction = report.primary_direction or state.first_direction
    scenario = state.scenarios.get(direction)
    return HorizonResult(
        bars=bars_count,
        outcome=state.lifecycle,
        triggered=bool(scenario and scenario.triggered_at),
        elapsed_bars=elapsed,
        utility=reward - config.waiting_cost_per_bar * elapsed,
        complete=state.observation_complete,
    ), result.data_quality


def replay_pair(prediction, incumbent, candidate, bars, as_of, config):
    first, quality1 = counterfactual(prediction, incumbent, bars, as_of, config)
    second, quality2 = counterfactual(prediction, candidate, bars, as_of, config)
    return PairedResult(
        prediction_id=prediction.id,
        incumbent=first,
        candidate=second,
        data_quality="OK" if quality1 == quality2 == "OK" else "AWAITING_DATA",
        bar_ids=tuple(b.id for b in bars),
    )


def paired_metrics(pairs):
    valid = [p for p in pairs if p.valid]
    n = len(valid)
    result = {"n": n, "unscored": len(pairs) - n, "gain": None, "failure_increase": None}
    for name in ("incumbent", "candidate"):
        values = [getattr(p, name) for p in valid]
        result[name] = {
            "achieved_rate": sum(v.outcome == "ACHIEVED" for v in values) / Decimal(n)
            if n
            else None,
            "failure_rate": sum(v.outcome == "INVALIDATED" for v in values) / Decimal(n)
            if n
            else None,
            "unresolved_rate": sum(v.outcome == "EXPIRED" for v in values) / Decimal(n)
            if n
            else None,
            "untriggered_rate": sum(not v.triggered for v in values) / Decimal(n) if n else None,
            "mean_wait_bars": sum((v.elapsed_bars for v in values), Decimal(0)) / n if n else None,
        }
    if n:
        result["gain"] = (
            sum((p.candidate.utility - p.incumbent.utility for p in valid), Decimal(0)) / n
        )
        result["failure_increase"] = (
            result["candidate"]["failure_rate"] - result["incumbent"]["failure_rate"]
        )
    return result


def should_promote(metrics, minimum_samples, config):
    return (
        metrics["n"] >= minimum_samples
        and metrics["gain"] >= config.minimum_score_gain
        and metrics["failure_increase"] <= config.maximum_failure_increase
    )


def should_rollback(metrics, config):
    return metrics["n"] >= config.monitor_batch_samples and (
        metrics["gain"] < -config.minimum_score_gain
        or metrics["failure_increase"] > config.maximum_failure_increase
    )


class HorizonService:
    def __init__(self, jobs, config, predictions):
        self.jobs, self.database = jobs, jobs.database
        self.config: PredictionConfig = config.prediction
        self.predictions = predictions

    @staticmethod
    def _scope(table, rule):
        return sa.and_(
            *(
                table.c[k] == rule[k]
                for k in ("engine_id", "symbol", "timeframe", "structure", "stage")
            )
        )

    async def run(self, attempt):
        """One scope per deterministic job. Fresh market data can be backfilled independently."""
        if attempt.kind != "HORIZON_LEARN":
            raise DomainError("FORBIDDEN", "期限规则只能由确定性学习任务变更", 403)
        rule_id = UUID(attempt.payload["rule_id"])
        async with self.jobs.transaction(attempt) as connection:
            rule = dict(
                (
                    await connection.execute(
                        sa.select(s.horizon_rules).where(s.horizon_rules.c.id == rule_id)
                    )
                )
                .mappings()
                .one()
            )
            if attempt.payload.get("engine_id") != str(rule["engine_id"]):
                raise DomainError("HORIZON_SCOPE", "期限任务engine不匹配", 403)
            now = await self.database.now(connection)
            trial = (
                (
                    await connection.execute(
                        sa.select(
                            s.horizon_trials,
                            s.horizon_trial_states.c.status,
                            s.horizon_trial_states.c.body.label("state_body"),
                            s.horizon_trial_states.c.revision,
                        )
                        .join(s.horizon_rules, s.horizon_rules.c.id == s.horizon_trials.c.rule_id)
                        .join(
                            s.horizon_trial_states,
                            s.horizon_trial_states.c.trial_id == s.horizon_trials.c.id,
                        )
                        .where(
                            self._scope(s.horizon_rules, rule),
                            s.horizon_trial_states.c.status.in_(("SHADOW", "ACTIVE")),
                        )
                        .order_by(s.horizon_trials.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            trial = dict(trial) if trial else None
            current_id = await connection.scalar(
                sa.select(s.horizon_heads.c.rule_id).where(self._scope(s.horizon_heads, rule))
            )
            if current_id != rule_id:
                return {"status": "SUPERSEDED_RULE"}
            records = (
                (
                    await connection.execute(
                        sa.select(s.prediction_facts)
                        .where(
                            s.prediction_facts.c.engine_id == rule["engine_id"],
                            s.prediction_facts.c.symbol == rule["symbol"],
                            s.prediction_facts.c.timeframe == rule["timeframe"],
                            s.prediction_facts.c.role == "TIMEFRAME",
                            s.prediction_facts.c.body["prediction"]["report"][
                                "structure"
                            ].as_string()
                            == rule["structure"],
                            s.prediction_facts.c.body["prediction"]["report"]["stage"].as_string()
                            == rule["stage"],
                            s.prediction_facts.c.published_at <= now,
                        )
                        .order_by(s.prediction_facts.c.published_at.desc())
                        .limit(200)
                    )
                )
                .mappings()
                .all()
            )
            predictions = [
                PublishedPrediction.model_validate(r["body"]["prediction"])
                for r in reversed(records)
            ]
            observed = set()
            if trial:
                observed = set(
                    (
                        await connection.execute(
                            sa.select(s.horizon_comparisons.c.prediction_id).where(
                                s.horizon_comparisons.c.trial_id == trial["id"],
                                s.horizon_comparisons.c.phase == trial["status"],
                            )
                        )
                    ).scalars()
                )
        if trial:
            return await self._advance(attempt, rule, trial, predictions, observed, now)
        return await self._propose(attempt, rule, predictions, now)

    async def _pairs(self, predictions, incumbent, candidate, now):
        pairs = []
        for prediction in predictions:
            end = prediction.published_at + timedelta(
                seconds=INTERVAL_SECONDS[prediction.report.timeframe] * max(incumbent, candidate)
            )
            if end > now:
                continue
            bars = await self.predictions.minute_bars(
                prediction.inst_id, prediction.observation_start, end, now
            )
            try:
                pairs.append(replay_pair(prediction, incumbent, candidate, bars, now, self.config))
            except ValueError:
                # A sloped price line can cease being positive at a longer counterfactual
                # horizon. Such a candidate cannot be evaluated from this fixed structure.
                continue
        return pairs

    async def _lock(self, connection, attempt, rule):
        await content_lock(
            connection,
            "horizon:"
            + content_hash(
                {k: rule[k] for k in ("engine_id", "symbol", "timeframe", "structure", "stage")}
            ),
        )
        await self.jobs.guard(connection, attempt)
        current = await connection.scalar(
            sa.select(s.horizon_heads.c.rule_id).where(self._scope(s.horizon_heads, rule))
        )
        if current != rule["id"]:
            raise DomainError("HORIZON_CONFLICT", "当前期限规则已经改变", 409)

    async def _save_pairs(self, connection, trial_id, phase, pairs, now):
        for pair in pairs:
            await connection.execute(
                insert(s.horizon_comparisons)
                .values(
                    trial_id=trial_id,
                    prediction_id=pair.prediction_id,
                    phase=phase,
                    body=json_value(pair),
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        s.horizon_comparisons.c.trial_id,
                        s.horizon_comparisons.c.prediction_id,
                        s.horizon_comparisons.c.phase,
                    ]
                )
            )

    async def _propose(self, attempt, rule, predictions, now):
        if len(predictions) < self.config.replay_min_samples:
            return {"status": "INSUFFICIENT_REPLAY", "available_predictions": len(predictions)}
        choices = []
        for candidate in self.config.horizon_candidates:
            if candidate == rule["bars"]:
                continue
            pairs = await self._pairs(predictions, rule["bars"], candidate, now)
            metrics = paired_metrics(pairs)
            if should_promote(metrics, self.config.replay_min_samples, self.config):
                choices.append((metrics["gain"], candidate, pairs, metrics))
        if not choices:
            return {"status": "NO_QUALIFIED_CANDIDATE"}
        _, candidate, pairs, metrics = sorted(choices, key=lambda c: (-c[0], c[1]))[0]
        dataset_hash = content_hash([p.prediction_id for p in pairs if p.valid])
        async with self.jobs.transaction(attempt) as connection:
            await self._lock(connection, attempt, rule)
            pending = await connection.scalar(
                sa.select(s.horizon_trials.c.id)
                .join(s.horizon_rules, s.horizon_rules.c.id == s.horizon_trials.c.rule_id)
                .join(
                    s.horizon_trial_states,
                    s.horizon_trial_states.c.trial_id == s.horizon_trials.c.id,
                )
                .where(
                    self._scope(s.horizon_rules, rule),
                    s.horizon_trial_states.c.status.in_(("SHADOW", "ACTIVE")),
                )
                .limit(1)
            )
            if pending:
                return {"status": "EXISTING_TRIAL", "trial_id": str(pending)}
            # A failed candidate cannot recycle the very same replay dataset indefinitely.
            existing = await connection.scalar(
                sa.select(s.horizon_trials.c.id).where(
                    s.horizon_trials.c.rule_id == rule["id"],
                    s.horizon_trials.c.candidate_bars == candidate,
                    s.horizon_trials.c.body["dataset_hash"].as_string() == dataset_hash,
                )
            )
            if existing:
                return {"status": "EXISTING_TRIAL", "trial_id": str(existing)}
            ident = uuid4()
            await connection.execute(
                insert(s.horizon_trials).values(
                    id=ident,
                    rule_id=rule["id"],
                    candidate_bars=candidate,
                    created_at=now,
                    body=json_value(
                        {
                            "incumbent_bars": rule["bars"],
                            "replay_metrics": metrics,
                            "dataset_hash": dataset_hash,
                            "attempt_id": attempt.id,
                            "waiting_cost_per_bar": self.config.waiting_cost_per_bar,
                        }
                    ),
                )
            )
            await connection.execute(
                insert(s.horizon_trial_states).values(
                    trial_id=ident, status="SHADOW", body={}, updated_at=now
                )
            )
            await self._save_pairs(connection, ident, "REPLAY", pairs, now)
            return {
                "status": "SHADOW",
                "trial_id": str(ident),
                "candidate_bars": candidate,
                "metrics": json_value(metrics),
            }

    async def _advance(self, attempt, rule, trial, predictions, observed, now):
        state = trial["state_body"]
        since = (
            datetime.fromisoformat(state["promoted_at"].replace("Z", "+00:00"))
            if trial["status"] == "ACTIVE"
            else trial["created_at"]
        )
        predictions = [
            p
            for p in predictions
            if p.published_at > since
            and p.id not in observed
            and p.report.horizon_rule_id == rule["id"]
        ]
        pairs = await self._pairs(
            predictions, trial["body"]["incumbent_bars"], trial["candidate_bars"], now
        )
        valid = [p for p in pairs if p.valid]
        async with self.jobs.transaction(attempt) as connection:
            await self._lock(connection, attempt, rule)
            projection = (
                (
                    await connection.execute(
                        sa.select(s.horizon_trial_states).where(
                            s.horizon_trial_states.c.trial_id == trial["id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            if projection["revision"] != trial["revision"]:
                raise DomainError("HORIZON_CONFLICT", "期限试验已经更新", 409)
            # Store complete observations only; missing/ambiguous samples remain visible in
            # forecast evaluations and can be retried, never counted toward the required N.
            await self._save_pairs(connection, trial["id"], trial["status"], valid, now)
            stored = (
                (
                    await connection.execute(
                        sa.select(s.horizon_comparisons.c.body)
                        .join(
                            s.prediction_facts,
                            s.prediction_facts.c.id == s.horizon_comparisons.c.prediction_id,
                        )
                        .where(
                            s.horizon_comparisons.c.trial_id == trial["id"],
                            s.horizon_comparisons.c.phase == trial["status"],
                        )
                        .order_by(s.prediction_facts.c.published_at, s.prediction_facts.c.id)
                    )
                )
                .scalars()
                .all()
            )
            stored = [PairedResult.model_validate(p) for p in stored]
            checked = state.get("checked_samples", 0)
            batch_size = (
                self.config.shadow_min_samples
                if trial["status"] == "SHADOW"
                else self.config.monitor_batch_samples
            )
            batch = stored[checked : checked + batch_size]
            metrics = paired_metrics(batch)
            status = trial["status"]
            update = {
                **state,
                "available_samples": len(stored),
                "pending_unscored": len(pairs) - len(valid),
            }
            if len(batch) >= batch_size:
                if status == "SHADOW":
                    if should_promote(metrics, batch_size, self.config):
                        new_id = await self._activate(
                            connection,
                            attempt,
                            rule,
                            trial["candidate_bars"],
                            "SHADOW_PROMOTED",
                            trial,
                            metrics,
                            batch,
                            now,
                        )
                        status = "ACTIVE"
                        update = {
                            "promoted_rule_id": str(new_id),
                            "promoted_at": json_value(now),
                            "checked_samples": 0,
                            "shadow_metrics": json_value(metrics),
                        }
                    else:
                        status = "REJECTED"
                        update["shadow_metrics"] = json_value(metrics)
                else:
                    update["checked_samples"] = checked + batch_size
                    update["last_monitor_metrics"] = json_value(metrics)
                    if should_rollback(metrics, self.config):
                        new_id = await self._activate(
                            connection,
                            attempt,
                            rule,
                            trial["body"]["incumbent_bars"],
                            "SHADOW_ROLLBACK",
                            trial,
                            metrics,
                            batch,
                            now,
                        )
                        status = "ROLLED_BACK"
                        update["rollback_rule_id"] = str(new_id)
            await connection.execute(
                sa.update(s.horizon_trial_states)
                .where(s.horizon_trial_states.c.trial_id == trial["id"])
                .values(
                    status=status,
                    revision=trial["revision"] + 1,
                    body=json_value(update),
                    updated_at=now,
                )
            )
            await self.jobs.guard(connection, attempt)
            return {"status": status, "metrics": json_value(metrics)}

    async def _activate(self, connection, attempt, rule, bars, reason, trial, metrics, batch, now):
        ident = uuid4()
        await connection.execute(
            insert(s.horizon_rules).values(
                id=ident,
                **{k: rule[k] for k in ("engine_id", "symbol", "timeframe", "structure", "stage")},
                bars=bars,
                previous_id=rule["id"],
                reason=reason,
                available_at=now,
                body=json_value(
                    {
                        "trial_id": trial["id"],
                        "attempt_id": attempt.id,
                        "metrics": metrics,
                        "prediction_ids": [p.prediction_id for p in batch],
                    }
                ),
            )
        )
        await connection.execute(
            sa.update(s.horizon_heads)
            .where(self._scope(s.horizon_heads, rule))
            .values(rule_id=ident)
        )
        return ident
