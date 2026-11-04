from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from test_prediction_pipeline import minute, publish_child, report, task
from test_prediction_pipeline import prediction_system as prediction_system
from trading_core.ai.analyzer import SocialAIAnalyzer
from trading_core.parameters import TradeParameters as EmaRsiAtrConfig
from trading_core.risk.engine import RiskLimits
from trading_core.schemas import AccountState, RuntimeState

from trading_agent.domain.errors import DomainError
from trading_agent.execution.service import ExecutionService
from trading_agent.execution.settings import ExecutionSettings
from trading_agent.execution.transport import OrderObservation, PaperTransport
from trading_agent.prediction.contracts import ParentDecision
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar

pytestmark = pytest.mark.postgres


async def opening(system, symbol="BTCUSDT"):
    child = await publish_child(system, symbol=symbol)
    from conftest import bars_at

    parent = await system[2].claim(("PREDICT_PARENT",))
    assert parent.payload["symbol"] == symbol
    bars = tuple(
        b.model_copy(update={"inst_id": symbol.removesuffix("USDT") + "-USDT-SWAP"})
        for b in bars_at(system[1].value.replace(minute=0, second=0, microsecond=0), "1h")
    )
    await system[3].freeze(parent, system[4], bars)
    context = await system[3].context(parent)
    prediction = await report(system, parent)
    decision = ParentDecision(
        prediction=prediction,
        action="OPEN_LONG",
        trade_style="TREND",
        selected_direction="UP",
        child_prediction_ids=[child],
        context_revision=context["context_revision"],
        relationship="fixture synthesis",
    )
    ident = await system[3].publish(parent, decision)
    await system[2].finish(parent, result_id=ident)
    await fresh_market(system, symbol)
    return ident


async def fresh_market(system, symbol="BTCUSDT", close="182"):
    db, clock, _, _, engine = system
    end = clock.value.replace(second=0, microsecond=0)
    inst = symbol.removesuffix("USDT") + "-USDT-SWAP"
    async with db.transaction() as c:
        await persist_bar(
            c,
            minute(
                inst,
                end - timedelta(minutes=1),
                high=str(Decimal(close) + 1),
                low=str(Decimal(close) - 1),
                close=close,
            ),
            symbol,
        )
        await c.execute(
            insert(s.prediction_streams)
            .values(
                engine_id=engine,
                symbol=symbol,
                cursor=end,
                body={"data_quality": "OK"},
                updated_at=clock.value,
            )
            .on_conflict_do_update(
                index_elements=[s.prediction_streams.c.engine_id, s.prediction_streams.c.symbol],
                set_={"cursor": end, "body": {"data_quality": "OK"}, "updated_at": clock.value},
            )
        )


def executor(system, transport=None, enabled=True):
    db, clock, jobs, predictions, engine = system
    config = predictions.config.model_copy(
        update={
            "prediction": predictions.config.prediction.model_copy(
                update={"execution_enabled": enabled}
            )
        }
    )

    async def social(symbol, market):
        return SocialAIAnalyzer(None, "fixture")._no_recent_data(symbol.removesuffix("USDT"))

    settings = ExecutionSettings(
        "fixture-account",
        RiskLimits(min_position_margin=1, max_leverage=3),
        EmaRsiAtrConfig(),
        RuntimeState(),
        AccountState(equity=1000),
        social,
    )
    return ExecutionService(
        jobs, config, predictions, engine, settings, transport or PaperTransport()
    )


async def records(system):
    async with system[0].engine.connect() as c:
        return {
            table.name: (await c.execute(sa.select(table))).mappings().all()
            for table in (
                s.trade_intents,
                s.trade_intent_states,
                s.trading_positions,
                s.trading_accounts,
                s.execution_events,
            )
        }


async def test_parent_to_risk_account_and_bound_close_no_reverse(prediction_system):
    system = prediction_system
    await opening(system)
    service = executor(system)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    await service.run(attempt)
    await system[2].finish(attempt)
    rows = await records(system)
    assert rows["trade_intent_states"][0]["status"] == "FILLED"
    position = rows["trading_positions"][0]
    assert position["status"] == "OPEN" and position["body"]["direction"] == "UP"
    account = rows["trading_accounts"][0]["body"]["account"]
    assert set(account["open_positions"]) == {"BTCUSDT"}
    assert account["open_position_signal_source"]["BTCUSDT"] == "prediction_parent"
    assert len([e for e in rows["execution_events"] if e["body"]["kind"] == "FILL_ACCOUNTING"]) == 1
    # Parent reversal proposes only a bound close. It does not open the opposite side.
    parent = await task(system, kind="PREDICT_PARENT")
    ctx = await system[3].context(parent)
    pred = (await report(system, parent)).model_copy(update={"primary_direction": None})
    decision = ParentDecision(
        prediction=pred,
        action="CLOSE",
        trade_style="TREND",
        selected_direction="DOWN",
        child_prediction_ids=[c["id"] for c in ctx["children"]],
        context_revision=ctx["context_revision"],
        position_id=position["position_id"],
        relationship="fixture reversal",
    )
    await system[3].publish(parent, decision)
    await system[2].finish(parent)
    close = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(close)
    rows = await records(system)
    assert rows["trading_positions"][0]["status"] == "CLOSED"
    assert rows["trading_accounts"][0]["body"]["account"]["open_positions"] == {}
    assert [r["action"] for r in rows["trade_intents"]] == ["OPEN_LONG", "CLOSE"]


class UncertainTransport(PaperTransport):
    def __init__(self):
        self.sends = 0
        self.queries = 0
        self.fraction = Decimal(".4")
        self.missing = True

    async def submit(self, order):
        self.sends += 1
        raise TimeoutError("fixture accepted order but network timeout")

    async def lookup(self, order):
        self.queries += 1
        if self.missing:
            return None
        return OrderObservation(
            state="filled" if self.fraction == 1 else "partially_filled",
            filled=Decimal(order["quantity"]) * self.fraction,
            average_price=order["price"],
        )


async def test_unknown_restart_queries_partial_fills_apply_once_and_reserve_account(
    prediction_system,
):
    system = prediction_system
    await opening(system)
    transport = UncertainTransport()
    service = executor(system, transport)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    with pytest.raises(DomainError, match="订单结果未知"):
        await service.run(attempt)
    # Same persistent journal, new service process and no in-memory sent-ID cache.
    service = executor(system, transport)
    with pytest.raises(DomainError, match="订单结果未知"):
        await service.run(attempt)
    assert transport.sends == 1 and transport.queries == 1
    transport.missing = False
    for _ in range(2):
        with pytest.raises(DomainError, match="订单待成交"):
            await service.run(attempt)
    rows = await records(system)
    partial = rows["trading_accounts"][0]["body"]["account"]["open_positions"]["BTCUSDT"]
    assert len([e for e in rows["execution_events"] if e["body"]["kind"] == "FILL_ACCOUNTING"]) == 1
    # Another symbol cannot size against a partially unreserved account.
    await opening(system, "ETHUSDT")
    other = await system[2].claim(("PREDICT_EXECUTE",))
    with pytest.raises(DomainError, match="前一个订单"):
        await service.run(other)
    transport.fraction = Decimal(1)
    await service.run(attempt)
    rows = await records(system)
    assert rows["trading_accounts"][0]["body"]["account"]["open_positions"][
        "BTCUSDT"
    ] == pytest.approx(partial / 0.4)
    assert transport.sends == 1
    assert len([e for e in rows["execution_events"] if e["body"]["kind"] == "FILL_ACCOUNTING"]) == 2


async def test_stale_data_and_expired_prediction_do_not_submit(prediction_system):
    system = prediction_system
    await opening(system)
    service = executor(system)
    system[1].value += timedelta(minutes=2)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    rows = await records(system)
    assert rows["trade_intent_states"][0]["status"] == "STALE"
    assert rows["trading_positions"] == []


async def test_disabled_execution_preserves_intent_without_account_or_order(prediction_system):
    await opening(prediction_system)
    service = executor(prediction_system, enabled=False)
    attempt = await prediction_system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    rows = await records(prediction_system)
    assert rows["trade_intent_states"][0]["status"] == "DISABLED"
    assert not rows["trading_accounts"] and not rows["trading_positions"]


async def close_intent(system):
    from trading_agent.prediction.contracts import PublishedPrediction

    async with system[0].transaction() as c:
        position = (
            (
                await c.execute(
                    sa.select(s.trading_positions).where(s.trading_positions.c.status == "OPEN")
                )
            )
            .mappings()
            .one()
        )
        fact = await c.scalar(
            sa.select(s.prediction_facts.c.body).where(
                s.prediction_facts.c.id == UUID(position["body"]["structure_prediction_id"])
            )
        )
        await system[3]._intent(
            c,
            PublishedPrediction.model_validate(fact["prediction"]),
            "CLOSE",
            f"fixture-close:{uuid4()}",
            position,
            system[1].value,
        )
    return await system[2].claim(("PREDICT_EXECUTE",))


async def test_partial_close_keeps_remaining_contracts_and_old_close_cannot_close_new_position(
    prediction_system,
):
    system = prediction_system
    await opening(system)
    service = executor(system)
    opening_attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(opening_attempt)
    await system[2].finish(opening_attempt)
    initial = (await records(system))["trading_accounts"][0]["body"]["account"][
        "open_position_contracts"
    ]["BTCUSDT"]
    close = await close_intent(system)
    transport = UncertainTransport()
    transport.missing = False
    service = executor(system, transport)
    with pytest.raises(DomainError):
        await service.run(close)
    with pytest.raises(DomainError, match="订单待成交"):
        await service.run(close)
    remaining = (await records(system))["trading_accounts"][0]["body"]["account"][
        "open_position_contracts"
    ]["BTCUSDT"]
    assert remaining == pytest.approx(initial * 0.6)
    transport.fraction = Decimal(1)
    await service.run(close)
    await system[2].finish(close)
    assert (await records(system))["trading_accounts"][0]["body"]["account"]["open_positions"] == {}
    # A pending intent bound to a replaced identity is terminal STALE; no transport send.
    async with system[0].transaction() as c:
        old = (
            (await c.execute(sa.select(s.trade_intents).where(s.trade_intents.c.action == "CLOSE")))
            .mappings()
            .one()
        )
        new = uuid4()
        await c.execute(
            sa.insert(s.trade_intents).values(**{**dict(old), "id": new, "cause_key": str(new)})
        )
        await c.execute(
            sa.insert(s.trade_intent_states).values(intent_id=new, status="PENDING", body={})
        )
        await c.execute(
            sa.update(s.trading_positions).values(position_id=uuid4(), status="OPEN", revision=99)
        )
        await system[2].create(
            c, "PREDICT_EXECUTE", str(new), {"intent_id": str(new)}, system[1].value
        )
    stale = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(stale)
    rows = await records(system)
    assert (
        next(r for r in rows["trade_intent_states"] if r["intent_id"] == new)["body"]["reason"]
        == "POSITION_CHANGED"
    )
    assert transport.sends == 1


async def test_exchange_native_exit_requires_unique_fill_evidence_and_is_idempotent(
    prediction_system,
):
    system = prediction_system
    await opening(system)
    service = executor(system)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    await system[2].finish(attempt)
    rows = await records(system)
    qty = rows["trading_positions"][0]["body"]["contracts"]
    snapshot = {
        "equity": 1001,
        "positions": {
            "BTCUSDT": {"quantity": qty, "position_key": "fixture-position", "mark_price": 182}
        },
        "closed": [],
    }

    async def read():
        return snapshot

    service.settings.snapshot_provider = read
    check = await task(system, kind="PREDICT_VERIFY", freeze=False)
    await service._reconcile(check)
    snapshot["positions"] = {}
    with pytest.raises(DomainError, match="退出事实"):
        await service._reconcile(check)
    assert (await records(system))["trading_positions"][0]["status"] == "OPEN"
    snapshot["closed"] = [
        dict(
            symbol="BTCUSDT",
            position_key="fixture-position",
            direction="long",
            contracts=qty,
            price=184,
            net_pnl=1,
            created_ms=int(system[1].value.timestamp() * 1000),
            updated_ms=int(system[1].value.timestamp() * 1000),
        )
    ]
    await service._reconcile(check)
    await service._reconcile(check)
    rows = await records(system)
    assert rows["trading_positions"][0]["status"] == "CLOSED"
    assert rows["trading_accounts"][0]["body"]["account"]["realized_pnl_amount"] == 1
    assert len([e for e in rows["execution_events"] if e["body"]["kind"] == "EXCHANGE_EXIT"]) == 1


async def test_expired_attempt_cannot_send_or_apply_confirmed_order(prediction_system):
    system = prediction_system
    await opening(system)
    transport = UncertainTransport()
    service = executor(system, transport)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    system[1].value += timedelta(seconds=61)
    with pytest.raises(DomainError):
        await service.run(attempt)
    assert transport.sends == 0


async def test_expired_partial_order_cancels_remainder_but_keeps_confirmed_position(
    prediction_system,
):
    system = prediction_system
    await opening(system)
    transport = UncertainTransport()
    service = executor(system, transport)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    with pytest.raises(DomainError):
        await service.run(attempt)
    # Real lease takeover after restart, same intent and stable client ID.
    system[1].value += timedelta(hours=4, minutes=1)
    resumed = await system[2].claim(("PREDICT_EXECUTE",))
    assert resumed.id != attempt.id and resumed.fence > attempt.fence
    transport.missing = False
    canceled = []

    async def cancel(order):
        canceled.append(order["client_id"])
        return OrderObservation(
            state="canceled",
            filled=Decimal(order["quantity"]) * Decimal(".4"),
            average_price=order["price"],
        )

    transport.cancel = cancel
    await service.run(resumed)
    rows = await records(system)
    assert rows["trade_intent_states"][0]["status"] == "CANCELED"
    assert rows["trading_positions"][0]["status"] == "OPEN"
    assert len(rows["trade_intents"]) == 1  # Expiry did not create a close.
    assert transport.sends == 1 and len(canceled) == 1


async def test_account_hard_stop_is_durable_and_blocks_other_symbol(prediction_system):
    system = prediction_system
    await opening(system)
    service = executor(system)
    service.settings.initial_account.daily_pnl_pct = -0.04
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    await system[2].finish(attempt)
    rows = await records(system)
    assert rows["trade_intent_states"][0]["status"] == "REJECTED"
    assert rows["trading_accounts"][0]["body"]["risk_halt"]["checks"] == {
        "daily_loss_limit": "fail"
    }
    await opening(system, "ETHUSDT")
    another = await system[2].claim(("PREDICT_EXECUTE",))
    await executor(system).run(another)
    rows = await records(system)
    assert any(r["body"].get("reason") == "ACCOUNT_RISK_HALT" for r in rows["trade_intent_states"])
    assert not rows["trading_positions"]


async def test_canceled_partial_close_fee_revision_counts_loss_only_after_entire_position_closes(
    prediction_system,
):
    system = prediction_system
    await opening(system)
    service = executor(system)
    entry = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(entry)
    await system[2].finish(entry)
    await fresh_market(system, close="181")
    close = await close_intent(system)
    transport = UncertainTransport()
    transport.missing = False
    service = executor(system, transport)
    with pytest.raises(DomainError):
        await service.run(close)
    with pytest.raises(DomainError):
        await service.run(close)
    partial = (await records(system))["trading_positions"][0]["body"]["realized"]

    async def canceled(order):
        return OrderObservation(
            state="canceled",
            filled=Decimal(order["quantity"]) * Decimal(".4"),
            average_price=order["price"],
            fee="-.1",
        )

    transport.lookup = canceled
    await service.run(close)
    await system[2].finish(close)
    rows = await records(system)
    assert rows["trading_accounts"][0]["body"]["account"]["consecutive_losses"] == 0
    assert rows["trading_positions"][0]["body"]["realized"] == pytest.approx(partial - 0.1)
    rest = await close_intent(system)
    await executor(system).run(rest)
    rows = await records(system)
    assert rows["trading_positions"][0]["status"] == "CLOSED"
    assert rows["trading_accounts"][0]["body"]["account"]["consecutive_losses"] == 1
