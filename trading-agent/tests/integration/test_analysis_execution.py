import pytest
import sqlalchemy as sa
from test_prediction_execution import executor, opening, records
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import report, task

from trading_agent.execution.transport import PaperTransport
from trading_agent.prediction.contracts import ParentDecision
from trading_agent.prediction.rounds import AnalysisRounds
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def unavailable(system):
    db, clock, _, predictions, engine = system
    rounds = AnalysisRounds(predictions)
    async with db.transaction() as c:
        await rounds.scope(c, engine, "BTCUSDT", clock.value)
        await c.execute(
            sa.update(s.analysis_scopes).values(
                state="ERROR", parent_available=False, error_code="AGENT_TIMEOUT"
            )
        )


async def test_error_stops_old_open_intent_without_submitting_order(prediction_system):
    system = prediction_system
    await opening(system)
    await unavailable(system)

    class NoOrders(PaperTransport):
        async def submit(self, order):
            raise AssertionError("An unavailable parent must not reach transport")

    service = executor(system, NoOrders())
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    async with system[0].engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.trading_positions)) == 0
        state = (await c.execute(sa.select(s.trade_intent_states))).mappings().one()
        assert state["status"] == "STALE" and state["body"]["reason"] == "PARENT_UNAVAILABLE"
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 2


async def test_error_does_not_block_existing_bound_close_or_falsify_structure(prediction_system):
    system = prediction_system
    await opening(system)
    service = executor(system)
    entry = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(entry)
    await system[2].finish(entry)
    position = (await records(system))["trading_positions"][0]
    parent = await task(system, kind="PREDICT_PARENT")
    context = await system[3].context(parent)
    decision = ParentDecision(
        prediction=(await report(system, parent)).model_copy(update={"primary_direction": None}),
        action="CLOSE",
        trade_style="TREND",
        selected_direction="DOWN",
        child_prediction_ids=[c["id"] for c in context["children"]],
        context_revision=context["context_revision"],
        position_id=position["position_id"],
        relationship="fixture bound structural/reversal exit",
    )
    await system[3].publish(parent, decision)
    await system[2].finish(parent)
    await unavailable(system)
    close = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(close)
    assert (await records(system))["trading_positions"][0]["status"] == "CLOSED"
    async with system[0].engine.connect() as c:
        assert all(
            body["lifecycle"] != "INVALIDATED"
            for body in (await c.execute(sa.select(s.prediction_states.c.body))).scalars()
        )
