"""Real PG promotion/rollback transactions with deterministic minute-history fixtures."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_prediction_pipeline import minute, publish_child
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.prediction.contracts import PublishedPrediction
from trading_agent.prediction.horizons import HorizonService
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def historical_samples(system, template, count, first_at, rule_id):
    db, _, jobs, _, engine = system
    facts = []
    async with db.transaction() as conn:
        for i in range(count):
            published = first_at + timedelta(days=i)
            job_id = await jobs.create(
                conn,
                "PREDICT_TIMEFRAME",
                str(uuid4()),
                {"engine_id": str(engine), "symbol": "BTCUSDT", "timeframe": "1h"},
                published,
            )
            await conn.execute(
                sa.update(s.jobs).where(s.jobs.c.id == job_id).values(state="SUCCEEDED")
            )
            attempt_id, input_id = uuid4(), uuid4()
            await conn.execute(
                sa.insert(s.job_attempts).values(
                    id=attempt_id,
                    job_id=job_id,
                    fence=1,
                    runtime_instance_id=jobs.ownership.instance_id,
                    runtime_generation=jobs.ownership.generation,
                    status="SUCCEEDED",
                    token_revoked=True,
                    started_at=published,
                    ended_at=published,
                )
            )
            await conn.execute(
                sa.insert(s.prediction_inputs).values(
                    id=input_id,
                    job_id=job_id,
                    engine_id=engine,
                    symbol="BTCUSDT",
                    timeframe="1h",
                    role="TIMEFRAME",
                    decision_at=published,
                    body={"fixture": True},
                    content_hash=content_hash({"fixture": str(job_id)}),
                )
            )
            value = PublishedPrediction.model_validate(
                {
                    **template.model_dump(),
                    "id": uuid4(),
                    "snapshot_id": input_id,
                    "published_at": published,
                    "observation_start": published,
                    "expires_at": published + timedelta(hours=template.report.horizon_bars),
                    "report": template.report.model_copy(update={"horizon_rule_id": rule_id}),
                }
            )
            body = json_value({"prediction": value, "submission": value.report})
            await conn.execute(
                sa.insert(s.prediction_facts).values(
                    id=value.id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    input_id=input_id,
                    engine_id=engine,
                    symbol="BTCUSDT",
                    timeframe="1h",
                    role="TIMEFRAME",
                    published_at=published,
                    expires_at=value.expires_at,
                    body=body,
                    content_hash=content_hash(body),
                )
            )
            facts.append(value)
    return facts


async def learn(system, learner, rule_id):
    db, clock, jobs, _, engine = system
    async with db.transaction() as conn:
        await jobs.create(
            conn,
            "HORIZON_LEARN",
            str(uuid4()),
            {"engine_id": str(engine), "rule_id": str(rule_id)},
            clock.value,
        )
    attempt = await jobs.claim(("HORIZON_LEARN",))
    result = await learner.run(attempt)
    await jobs.finish(attempt)
    return result


async def test_replay_shadow_promotion_restart_monitor_and_rollback(prediction_system, monkeypatch):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    first = await publish_child(system)
    async with db.engine.connect() as conn:
        body = await conn.scalar(
            sa.select(s.prediction_facts.c.body).where(s.prediction_facts.c.id == first)
        )
    template = PublishedPrediction.model_validate(body["prediction"])
    original_id = template.report.horizon_rule_id
    learner = HorizonService(jobs, predictions.config, predictions)
    original_clock = clock.value
    first_at = original_clock.replace(minute=0, second=0) - timedelta(days=42)
    historical = await historical_samples(system, template, 40, first_at, original_id)
    outcomes = {p.observation_start: "ACHIEVED" for p in historical}

    async def history(inst_id, start, end, as_of):
        if start not in outcomes:
            return ()
        length = int((end - start).total_seconds() // 60)
        bad = outcomes[start] == "INVALIDATED"
        return tuple(
            minute(
                inst_id,
                start + timedelta(minutes=i),
                high="205" if i >= 300 and not bad else "185",
                low="90" if i >= 300 and bad else "177",
                close="95" if i >= 300 and bad else "182",
            )
            for i in range(length)
        )

    monkeypatch.setattr(predictions, "minute_bars", history)
    first_hashes = [content_hash(p) for p in historical]
    result = await learn(system, learner, original_id)
    assert result["status"] == "SHADOW" and result["candidate_bars"] == 6
    trial_id = UUID(result["trial_id"])
    assert result["metrics"]["n"] == 40
    assert (await learn(system, learner, original_id))["status"] == "SHADOW"
    # Forward samples must be strictly later than trial creation, not recycled replay evidence.
    first_shadow_at = original_clock.replace(minute=0, second=0) + timedelta(days=1)
    shadow = await historical_samples(system, template, 19, first_shadow_at, original_id)
    outcomes.update({p.observation_start: "ACHIEVED" for p in shadow})
    clock.value = first_shadow_at + timedelta(days=19)
    assert (await learn(system, learner, original_id))["status"] == "SHADOW"
    twentieth = await historical_samples(
        system, template, 1, first_shadow_at + timedelta(days=19), original_id
    )
    outcomes[twentieth[0].observation_start] = "ACHIEVED"
    clock.value += timedelta(days=1)
    result = await learn(system, HorizonService(jobs, predictions.config, predictions), original_id)
    assert result["status"] == "ACTIVE" and result["metrics"]["n"] == 20
    async with db.engine.connect() as conn:
        active = (
            (
                await conn.execute(
                    sa.select(s.horizon_rules).where(s.horizon_rules.c.previous_id == original_id)
                )
            )
            .mappings()
            .one()
        )
        assert active["bars"] == 6 and active["reason"] == "SHADOW_PROMOTED"
        current = await conn.scalar(
            sa.select(s.horizon_heads.c.rule_id).where(s.horizon_heads.c.rule_id == active["id"])
        )
        assert current == active["id"]
        # Publication stays byte-for-byte unchanged.
        saved = await conn.scalar(
            sa.select(s.prediction_facts.c.body).where(s.prediction_facts.c.id == first)
        )
        assert saved == body
    assert [content_hash(p) for p in historical] == first_hashes
    promoted_template = PublishedPrediction.model_validate(
        {
            **template.model_dump(),
            "report": template.report.model_copy(
                update={"horizon_bars": 6, "horizon_rule_id": active["id"]}
            ),
            "expires_at": template.published_at + timedelta(hours=6),
        }
    )
    first_monitor_at = clock.value.replace(minute=0, second=0) + timedelta(days=1)
    monitor = await historical_samples(
        system, promoted_template, 20, first_monitor_at, active["id"]
    )
    outcomes.update({p.observation_start: "INVALIDATED" for p in monitor})
    clock.value = first_monitor_at + timedelta(days=21)
    result = await learn(system, learner, active["id"])
    assert result["status"] == "ROLLED_BACK" and result["metrics"]["failure_increase"] == "1"
    async with db.engine.connect() as conn:
        rollback = (
            (
                await conn.execute(
                    sa.select(s.horizon_rules).where(s.horizon_rules.c.previous_id == active["id"])
                )
            )
            .mappings()
            .one()
        )
        assert rollback["bars"] == 4 and rollback["reason"] == "SHADOW_ROLLBACK"
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.horizon_comparisons)
                .where(s.horizon_comparisons.c.trial_id == trial_id)
            )
            == 80
        )
    assert (await learn(system, learner, active["id"]))["status"] == "SUPERSEDED_RULE"


async def test_insufficient_samples_and_attempt_authority(prediction_system):
    system = prediction_system
    first = await publish_child(system)
    async with system[0].engine.connect() as conn:
        body = await conn.scalar(
            sa.select(s.prediction_facts.c.body).where(s.prediction_facts.c.id == first)
        )
    rule_id = UUID(body["prediction"]["report"]["horizon_rule_id"])
    learner = HorizonService(system[2], system[3].config, system[3])
    result = await learn(system, learner, rule_id)
    assert result == {"status": "INSUFFICIENT_REPLAY", "available_predictions": 1}
    unauthorized = await system[2].claim(("PREDICT_PARENT",))
    with pytest.raises(DomainError, match="确定性学习任务"):
        await learner.run(unauthorized)
