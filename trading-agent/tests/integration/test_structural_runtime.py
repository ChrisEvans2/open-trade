import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_pipeline import Clock, minute, report

from trading_agent.api.auth import CredentialHashes
from trading_agent.config import AppConfig, HermesConfig, PredictionConfig, RuntimeConfig
from trading_agent.jobs.service import token_hash
from trading_agent.knowledge.contracts import KnowledgeReview
from trading_agent.notifications.outbox import Delivery
from trading_agent.prediction.contracts import ParentDecision
from trading_agent.runtime.agent_runner import AgentRunner
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.runtime.sandboxes import input_files
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database

pytestmark = pytest.mark.postgres


class FixtureResources:
    async def recover(self):
        pass

    async def cleanup(self, attempt):
        return True

    async def ready(self):
        return True  # fixture isolation only


class FixtureSender:
    async def send(self, target, body):
        return Delivery("SENT", "fixture-message")


async def until(predicate):
    # Real PG writes share ownership locks. Use a bounded completion wait,
    # separate from the production task/model deadlines.
    async with asyncio.timeout(60):
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.2)


class PublicMarket:
    def __init__(self, clock):
        self.clock = clock

    def now(self):
        return self.clock.value

    async def forming_candle(self, timeframe, deadline, *, inst_id):
        return None  # This fixture intentionally exercises confirmed-only fallback.

    async def check_clock(self, deadline):
        pass

    async def instrument(self, deadline, *, inst_id):
        return {}

    async def observations(self, deadline, **scope):
        return ()

    async def candles(self, anchor, timeframe, deadline, *, inst_id):
        return tuple(
            b.model_copy(update={"inst_id": inst_id, "id": uuid4()})
            for b in bars_at(anchor, timeframe)
        )

    async def minute_window(self, inst, start, end, deadline):
        values = []
        while start < end:
            values.append(minute(inst, start))
            start += timedelta(minutes=1)
        return tuple(values)

    async def close(self):
        pass


class FixturePredictionRunner(AgentRunner):
    async def run(self, attempt):
        root, _, _ = await self._inputs(attempt)
        bridged = dict(input_files(root))
        assert {
            "prediction-schema.json",
            "decision-schema.json",
            "review-schema.json",
        } <= bridged.keys()
        assert attempt.token not in bridged["context.json"].decode()
        engine = UUID(attempt.payload["engine_id"])
        if attempt.kind == "PREDICT_REVIEW":
            context = json.loads(bridged["context.json"])
            windows = context["review_market"]["bars_by_timeframe"]
            assert set(windows) == {"1h", "4h", "6h", "12h"}
            assert all(len(bars) == 80 for bars in windows.values())
            inst = context["symbol"].removesuffix("USDT") + "-USDT-SWAP"
            assert all(b["inst_id"] == inst for bars in windows.values() for b in bars)
            return await self.knowledge.submit(
                attempt, KnowledgeReview(narrative="fixture daily review", changes=[])
            )
        if attempt.payload.get("analysis_round"):
            from test_analysis_rounds import content

            from trading_agent.prediction.submissions import DecisionContent

            rounds = self.predictions.rounds
            # Models are fixtures here; exercise the real coordinated role API,
            # frozen inputs, publication and parent intent transaction.
            for role in ("1h", "4h", "6h", "12h", "parent"):
                stage = await rounds.begin(attempt, role)
                payload = content(stage["context"])
                if role == "parent":
                    payload = DecisionContent(
                        prediction=payload,
                        action="OPEN_LONG",
                        trade_style="TREND",
                        selected_direction="UP",
                        child_refs=["child.1h"],
                        relationship="fixture four-period synthesis",
                    )
                await rounds.complete_execution(attempt, stage["run"])
                await rounds.submit(attempt, stage["run"], payload)
            return await rounds.result(attempt)
        payload = await report((None, None, None, self.predictions, engine), attempt)
        if attempt.kind == "PREDICT_PARENT":
            ctx = await self.predictions.context(attempt)
            position = ctx["position"]
            payload = ParentDecision(
                prediction=payload,
                action="OPEN_LONG",
                trade_style="TREND",
                selected_direction="UP",
                child_prediction_ids=[c["id"] for c in ctx["children"]],
                context_revision=ctx["context_revision"],
                position_id=position["position_id"]
                if position and position["status"] == "OPEN"
                else None,
                relationship="fixture multi-timeframe synthesis",
            )
        return await self.predictions.publish(attempt, payload)


async def test_single_runtime_bootstraps_both_symbols_reviews_indexes_and_intents(
    postgres_url, anchor, tmp_path
):
    clock = Clock(anchor + timedelta(seconds=65))
    db = Database(postgres_url, clock=clock)
    config = AppConfig(
        hermes=HermesConfig(provider="fixture", model="fixture"),
        runtime=RuntimeConfig(data_dir=tmp_path / "runtime"),
        prediction=PredictionConfig(knowledge_index_path=tmp_path / "knowledge.sqlite"),
    )

    async def verified():
        return True  # fixture deployment, not real Cube acceptance

    fatal = []
    runtime = Runtime(
        config,
        db,
        CredentialHashes(token_hash("controller"), token_hash("operator")),
        FixtureResources(),
        PublicMarket(clock),
        verified,
        sender=FixtureSender(),
        runner_factory=FixturePredictionRunner,
        fatal_exit=lambda: fatal.append(True),
        # The previous 50-200ms polling periods repeatedly queued exclusive
        # scheduler locks while 10 inputs were frozen. This is a startup test,
        # not a lock-contention benchmark; retain concurrent recovery/delivery.
        loop_periods={"scheduler": 5, "executor": 0.2, "notifier": 1, "recovery": 2},
    )
    await runtime.start()
    try:

        async def done():
            async with db.engine.connect() as c:
                count = await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_heads))
                disabled = await c.scalar(
                    sa.select(sa.func.count())
                    .select_from(s.trade_intent_states)
                    .where(s.trade_intent_states.c.status == "DISABLED")
                )
                errors = (
                    await c.execute(
                        sa.select(s.jobs.c.kind, s.jobs.c.state, s.jobs.c.last_error).where(
                            s.jobs.c.state.in_(("FAILED", "SKIPPED", "RETRY_WAIT"))
                        )
                    )
                ).all()
                assert not errors, errors
                reviews = await c.scalar(
                    sa.select(sa.func.count())
                    .select_from(s.reviews)
                    .join(s.job_attempts, s.job_attempts.c.id == s.reviews.c.attempt_id)
                    .join(s.jobs, s.jobs.c.id == s.job_attempts.c.job_id)
                    .where(s.jobs.c.kind == "PREDICT_REVIEW")
                )
                return count == 10 and disabled >= 2 and reviews == 2

        # Advance the deterministic business clock beyond the fixed event window.
        # Keep both production-sized model slots occupied only through the runtime.
        clock.value += timedelta(seconds=30)
        await until(done)
        async with db.engine.connect() as c:
            scopes = (
                await c.execute(
                    sa.select(
                        s.prediction_heads.c.symbol,
                        s.prediction_heads.c.timeframe,
                        s.prediction_heads.c.role,
                    )
                )
            ).all()
            assert len(scopes) == 10
            assert await c.scalar(sa.select(sa.func.count()).select_from(s.forecasts)) == 0
            assert await c.scalar(sa.select(sa.func.count()).select_from(s.knowledge_versions)) == 4
            assert not await c.scalar(sa.select(sa.func.count()).select_from(s.trading_accounts))
        assert not fatal
        assert runtime.api_services().predictions is runtime.predictions
    finally:
        await runtime.stop()
