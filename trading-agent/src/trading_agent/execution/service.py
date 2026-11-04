"""Durable intent/order/account transaction boundary.

Only this deterministic service calls transports. It reserves the account before
I/O, persists SENDING before sending, and reconciles cumulative observations after
restart. An unknown result is never interpreted as permission to submit again.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from trading_core.prediction_adapter import PredictionRiskAdapter, PreparedPrediction
from trading_core.schemas import (
    AccountState,
    ExecutionResult,
    MarketSnapshot,
    RiskDecision,
    StrategySignal,
)

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import boundary
from trading_agent.notifications.outbox import enqueue
from trading_agent.prediction.contracts import PriceLine, PublishedPrediction
from trading_agent.prediction.service import scope_filter
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "STALE", "DISABLED"}
INFLIGHT = ("PREPARED", "SENDING", "UNKNOWN", "PARTIAL", "WORKING")


class ExecutionService:
    def __init__(self, jobs, config, predictions, engine_id, settings=None, transport=None):
        self.jobs, self.database, self.config = jobs, jobs.database, config
        self.predictions, self.engine_id = predictions, engine_id
        self.settings, self.transport = settings, transport
        self.io_lock = asyncio.Lock()
        self.account_key = settings.account_key if settings else "disabled"

    async def validate_engine(self):
        if not self.config.prediction.execution_enabled:
            return
        async with self.database.transaction() as c:
            await self.jobs.ownership.guard(c)
            await self._lock(c)
            row = (
                (
                    await c.execute(
                        sa.select(s.trading_accounts).where(
                            s.trading_accounts.c.account_key == self.account_key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row and row["body"]["engine_id"] != str(self.engine_id):
                inflight = await c.scalar(
                    sa.select(s.trade_intent_states.c.intent_id)
                    .where(s.trade_intent_states.c.status.in_(INFLIGHT))
                    .limit(1)
                )
                if row["body"]["account"]["open_positions"] or inflight:
                    raise DomainError(
                        "ENGINE_CUTOVER_REQUIRES_FLAT",
                        "切换预测引擎版本前须由原入口完成持仓和未决订单处理",
                        503,
                    )
                await c.execute(
                    sa.update(s.trading_accounts)
                    .where(s.trading_accounts.c.account_key == self.account_key)
                    .values(
                        body={**row["body"], "engine_id": str(self.engine_id)},
                        revision=row["revision"] + 1,
                    )
                )

    async def _lock(self, c):
        await content_lock(c, "execution-account:" + self.account_key)

    async def _account(self, c, now):
        row = (
            (
                await c.execute(
                    sa.select(s.trading_accounts)
                    .where(s.trading_accounts.c.account_key == self.account_key)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row), AccountState.model_validate(row["body"]["account"])
        account = self.settings.initial_account.model_copy(deep=True)
        if account.open_positions:
            raise DomainError("ACCOUNT_ADOPTION_REQUIRED", "已有持仓需先完成账户迁移校验", 409)
        body = dict(
            account=account.model_dump(mode="json"),
            engine_id=str(self.engine_id),
            initial_equity=account.equity
            - account.realized_pnl_amount
            - account.total_unrealized_pnl_amount,
            peak_equity=account.equity / (1 - account.drawdown_pct)
            if account.drawdown_pct < 1
            else account.equity,
            day=str(now.date()),
            day_equity=account.equity / (1 + account.daily_pnl_pct)
            if account.daily_pnl_pct > -1
            else account.equity,
        )
        await c.execute(
            insert(s.trading_accounts).values(
                account_key=self.account_key, body=body, updated_at=now
            )
        )
        return dict(account_key=self.account_key, body=body, revision=1), account

    def _adapter(self, account, runtime=None):
        return PredictionRiskAdapter(
            self.settings.limits, self.settings.strategy, account, runtime or self.settings.runtime
        )

    async def _save_account(self, c, row, account, now):
        body = dict(row["body"])
        if self.transport.mode == "paper":
            account.equity = max(
                0,
                body["initial_equity"]
                + account.realized_pnl_amount
                + account.total_unrealized_pnl_amount,
            )
        if body["day"] != str(now.date()):
            body.update(day=str(now.date()), day_equity=account.equity)
        body["peak_equity"] = max(body["peak_equity"], account.equity)
        account.daily_pnl_pct = (
            account.equity / body["day_equity"] - 1 if body["day_equity"] else -1
        )
        account.drawdown_pct = (
            1 - account.equity / body["peak_equity"] if body["peak_equity"] else 1
        )
        body["account"] = account.model_dump(mode="json")
        await c.execute(
            sa.update(s.trading_accounts)
            .where(s.trading_accounts.c.account_key == self.account_key)
            .values(body=body, revision=s.trading_accounts.c.revision + 1, updated_at=now)
        )

    async def _event(self, c, attempt, intent, kind, body, now):
        event_key = f"{intent['id']}:{kind}:{content_hash(body)}"
        await c.execute(
            insert(s.execution_events)
            .values(
                intent_id=intent["id"],
                attempt_id=attempt.id,
                event_key=event_key,
                body=json_value(dict(kind=kind, **body)),
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[s.execution_events.c.event_key])
        )
        if kind in {"FILLED", "CANCELED", "REJECTED", "STALE", "UNKNOWN"}:
            await enqueue(
                c,
                key=f"execution:{event_key}",
                target=self.config.notification.target,
                body=f"交易执行状态：{kind}\n{intent['symbol']} {intent['action']}\n"
                f"intent_id={intent['id']}\n请通过主控查询意图详情与已保存的风控/成交事实。",
                now=now,
            )

    async def _state(self, c, attempt, intent, status, body, now):
        await c.execute(
            sa.update(s.trade_intent_states)
            .where(s.trade_intent_states.c.intent_id == intent["id"])
            .values(
                status=status,
                body=json_value(body),
                revision=s.trade_intent_states.c.revision + 1,
                updated_at=now,
            )
        )
        await self._event(c, attempt, intent, status, body, now)

    async def _read(self, c, attempt):
        if attempt.kind != "PREDICT_EXECUTE":
            raise DomainError("FORBIDDEN", "执行仅对确定性任务开放", 403)
        ident = UUID(attempt.payload["intent_id"])
        intent = (
            (await c.execute(sa.select(s.trade_intents).where(s.trade_intents.c.id == ident)))
            .mappings()
            .one()
        )
        state = (
            (
                await c.execute(
                    sa.select(s.trade_intent_states).where(
                        s.trade_intent_states.c.intent_id == ident
                    )
                )
            )
            .mappings()
            .one()
        )
        return dict(intent), dict(state)

    async def _position(self, c, intent):
        row = (
            (
                await c.execute(
                    sa.select(s.trading_positions).where(
                        scope_filter(s.trading_positions, intent["engine_id"], intent["symbol"])
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def _eligible(self, c, intent, now):
        await self.predictions._lock_symbol(c, intent["engine_id"], intent["symbol"])
        position = await self._position(c, intent)
        if intent["engine_id"] != self.engine_id:
            return "ENGINE_CHANGED"
        if intent["action"] == "CLOSE":
            if (
                not position
                or position["status"] != "OPEN"
                or position["position_id"] != intent["position_id"]
                or position["revision"] != intent["position_version"]
            ):
                return "POSITION_CHANGED"
            return None
        if position and position["status"] == "OPEN":
            return "POSITION_ALREADY_OPEN"
        account_body = await c.scalar(
            sa.select(s.trading_accounts.c.body).where(
                s.trading_accounts.c.account_key == self.account_key
            )
        )
        if account_body and account_body.get("risk_halt"):
            return "ACCOUNT_RISK_HALT"
        paused = await c.scalar(
            sa.select(s.runtime_control.c.paused).where(s.runtime_control.c.id == 1)
        )
        if (
            paused
            or self.settings.runtime.global_paused
            or self.settings.runtime.emergency
            or self.settings.runtime.mode.value in {"paused", "emergency"}
        ):
            return "ENTRIES_PAUSED"
        head = await c.scalar(
            sa.select(s.prediction_heads.c.prediction_id).where(
                scope_filter(s.prediction_heads, self.engine_id, intent["symbol"]),
                s.prediction_heads.c.role == "PARENT",
            )
        )
        fact = (
            (
                await c.execute(
                    sa.select(s.prediction_facts).where(
                        s.prediction_facts.c.id == intent["prediction_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        state = (
            (
                await c.execute(
                    sa.select(s.prediction_states).where(
                        s.prediction_states.c.prediction_id == head
                    )
                )
            )
            .mappings()
            .first()
        )
        stream = (
            (
                await c.execute(
                    sa.select(s.prediction_streams).where(
                        scope_filter(s.prediction_streams, self.engine_id, intent["symbol"])
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            rounds := getattr(self.predictions, "rounds", None)
        ) and not await rounds.parent_available(c, self.engine_id, intent["symbol"]):
            return "PARENT_UNAVAILABLE"
        if (
            head != intent["prediction_id"]
            or fact["expires_at"] <= now
            or not state
            or state["body"]["lifecycle"] != "ACTIVE"
        ):
            return "PREDICTION_ENDED_OR_REPLACED"
        if (
            state["data_quality"] != "OK"
            or datetime.fromisoformat(state["body"]["verified_until"]) < boundary(now, "1m")
            or not stream
            or stream["body"]["data_quality"] != "OK"
            or stream["cursor"] < boundary(now, "1m")
        ):
            return "MARKET_GAP"
        # A parent can be obsolete even before the new synthesis is published.
        original = fact["body"].get("submission")
        children, _, _ = await self.predictions._children(c, self.engine_id, intent["symbol"], now)
        if original and set(original["child_prediction_ids"]) != {p["id"] for p in children}:
            return "CHILD_CONTEXT_CHANGED"
        for child in children:
            verified = await c.scalar(
                sa.select(s.prediction_states.c.body).where(
                    s.prediction_states.c.prediction_id == UUID(child["id"])
                )
            )
            if datetime.fromisoformat(verified["verified_until"]) < boundary(now, "1m"):
                return "CHILD_VERIFICATION_PENDING"
        return None

    async def _market(self, c, symbol, now):
        row = (
            (
                await c.execute(
                    sa.select(s.market_bars)
                    .where(
                        s.market_bars.c.symbol == symbol,
                        s.market_bars.c.interval == "1m",
                        s.market_bars.c.close_boundary <= now,
                        s.market_bars.c.available_at <= now,
                    )
                    .order_by(
                        s.market_bars.c.close_boundary.desc(), s.market_bars.c.available_at.desc()
                    )
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if not row or now - row["close_boundary"] > timedelta(seconds=90):
            raise DomainError("EXECUTION_PRICE_STALE", "执行价格缺失或过期", 503)
        return MarketSnapshot(
            symbol=symbol,
            timeframe="1h",
            timestamp=row["close_boundary"],
            **{
                k: float(row[v])
                for k, v in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"))
            },
            volume=float(row["volume_base"]),
        )

    async def run(self, attempt):
        async with self.io_lock:
            if self.settings and self.settings.policy_loader:
                self.settings.policy_loader()
            return await self._run(attempt)

    async def _run(self, attempt):
        async with self.jobs.transaction(attempt) as c:
            await self._lock(c)
            now = await self.database.now(c)
            intent, state = await self._read(c, attempt)
            if state["status"] in TERMINAL:
                return intent["id"]
            if not self.config.prediction.execution_enabled:
                await self._state(
                    c, attempt, intent, "DISABLED", {"reason": "执行未启用；仅保留意图"}, now
                )
                return intent["id"]
            if not self.settings or not self.transport:
                raise DomainError("EXECUTION_NOT_CONFIGURED", "执行适配未配置", 503)
            row, account = await self._account(c, now)
            other = await c.scalar(
                sa.select(s.trade_intent_states.c.intent_id)
                .where(
                    s.trade_intent_states.c.intent_id != intent["id"],
                    s.trade_intent_states.c.status.in_(INFLIGHT),
                )
                .limit(1)
            )
            if other:
                raise DomainError("ACCOUNT_EXECUTION_BUSY", "先确认账户内前一个订单的结果", 503)
            if state["status"] == "PENDING":
                reason = await self._eligible(c, intent, now)
                if reason:
                    await self._state(c, attempt, intent, "STALE", {"reason": reason}, now)
                    return intent["id"]
                market = await self._market(c, intent["symbol"], now)
                fact = (
                    (
                        await c.execute(
                            sa.select(s.prediction_facts).where(
                                s.prediction_facts.c.id == intent["prediction_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                context = await c.scalar(
                    sa.select(s.prediction_inputs.c.body).where(
                        s.prediction_inputs.c.id == fact["input_id"]
                    )
                )
                control = await c.scalar(
                    sa.select(s.runtime_control.c.paused).where(s.runtime_control.c.id == 1)
                )
                account_revision = row["revision"]
            else:
                market = None
        if state["status"] == "PENDING":
            # Risk feeds and instrument lookup are external I/O, outside DB transactions.
            account = await self.settings.refresh(account)
            social = await self.settings.social(intent["symbol"], market)
            runtime = self.settings.runtime.model_copy(
                update={"global_paused": bool(control) or self.settings.runtime.global_paused}
            )
            adapter = self._adapter(account, runtime)
            if context.get("input_kind") == "CHILD_SYNTHESIS":
                basis = context.get("execution_basis")
                if not basis:
                    raise DomainError(
                        "RISK_DATA_MISSING", "缺少1h子预测已确认ATR，不能准备订单", 409
                    )
                atr = float(basis["atr_pct"])
            else:
                atr = float(next(f["value"] for f in context["features"] if f["name"] == "atr_pct"))
            bounds = {}
            prediction = PublishedPrediction.model_validate(fact["body"]["prediction"])
            style = intent["body"]["details"].get("trade_style", "TREND")
            if style == "RANGE":
                candidate = next(
                    (s for s in context["structures"] if s["id"] == prediction.report.structure_id),
                    None,
                )
                if candidate is None or candidate["kind"] != "BOX":
                    raise DomainError(
                        "RANGE_STRUCTURE_REQUIRED", "震荡风控需要已验证的箱体上下边界", 409
                    )
                bounds = {
                    f"range_{side}": float(
                        PriceLine.model_validate(candidate[key]).value_at(market.timestamp)
                    )
                    for side, key in (("low", "lower"), ("high", "upper"))
                }
            try:
                prepared = adapter.prepare(
                    intent["action"],
                    style,
                    market,
                    atr_pct=atr,
                    confidence=float(max(prediction.report.p_up, prediction.report.p_down)),
                    reason=prediction.report.reason,
                    social_risk=social,
                    **bounds,
                )
            except ValueError as exc:
                raise DomainError(
                    "RISK_PARAMETERS_INVALID", "预测的风险参数或几何边界无效", 409
                ) from exc
            order = (
                await self.transport.build(intent["id"], prepared, market, account)
                if prepared.decision.allow_trade
                else None
            )
            async with self.jobs.transaction(attempt) as c:
                await self._lock(c)
                now = await self.database.now(c)
                current, _ = await self._account(c, now)
                intent, state = await self._read(c, attempt)
                if current["revision"] != account_revision or state["status"] != "PENDING":
                    raise DomainError("ACCOUNT_CHANGED", "风控期间账户已变化", 503)
                reason = await self._eligible(c, intent, now)
                if reason or now - market.timestamp > timedelta(seconds=90):
                    await self._state(
                        c, attempt, intent, "STALE", {"reason": reason or "PRICE_STALE"}, now
                    )
                    return intent["id"]
                body = dict(
                    order=order,
                    prepared=dict(
                        signal=prepared.signal.model_dump(mode="json"),
                        decision=prepared.decision.model_dump(mode="json"),
                        trailing_stop_pct=prepared.trailing_stop_pct,
                        trailing_activation_pct=prepared.trailing_activation_pct,
                    ),
                    market=market.model_dump(mode="json"),
                    social=social.model_dump(mode="json"),
                    risk_policy=asdict(self.settings.limits),
                    strategy_parameters=asdict(self.settings.strategy),
                    applied_quantity="0",
                    applied_notional="0",
                    applied_fee="0",
                    position_id=str(uuid4())
                    if intent["action"] != "CLOSE"
                    else str(intent["position_id"]),
                )
                if current["revision"] == 1 and self.transport.mode == "live":
                    current["body"].update(
                        initial_equity=account.equity,
                        peak_equity=account.equity,
                        day_equity=account.equity,
                    )
                failed = {
                    k: v
                    for k, v in prepared.decision.risk_checks.items()
                    if k in {"daily_loss_limit", "drawdown_limit", "consecutive_losses", "health"}
                    and v == "fail"
                }
                if failed:
                    current["body"]["risk_halt"] = {"checks": failed, "at": json_value(now)}
                await self._save_account(c, current, account, now)
                await self._state(
                    c, attempt, intent, "PREPARED" if order else "REJECTED", body, now
                )
                state = dict(status="PREPARED" if order else "REJECTED", body=json_value(body))
            if not order:
                return intent["id"]
        body = state["body"]
        if state["status"] == "PREPARED":
            if self.settings.policy_loader:
                self.settings.policy_loader()
            # Recheck the lease/fence, freshness and bound position immediately before
            # recording the irreversible send authorization. No retry may re-enter it.
            async with self.jobs.transaction(attempt) as c:
                await self._lock(c)
                now = await self.database.now(c)
                reason = await self._eligible(c, intent, now)
                if intent["action"] != "CLOSE" and (
                    body["risk_policy"] != asdict(self.settings.limits)
                    or body["strategy_parameters"] != json_value(asdict(self.settings.strategy))
                ):
                    reason = "RISK_POLICY_CHANGED"
                if reason:
                    await self._state(c, attempt, intent, "STALE", {**body, "reason": reason}, now)
                    return intent["id"]
                await self._state(c, attempt, intent, "SENDING", body, now)
            send = True
        else:
            send = False
        try:
            observation = await (
                self.transport.submit(body["order"])
                if send
                else self.transport.lookup(body["order"])
            )
        except Exception:
            # Never persist private response bodies or exception text in the journal.
            observation = None
        if observation is None:
            async with self.jobs.transaction(attempt) as c:
                await self._lock(c)
                await self._state(c, attempt, intent, "UNKNOWN", body, await self.database.now(c))
            raise DomainError(
                "ORDER_RESULT_UNKNOWN", "订单结果未知；后续仅查询稳定客户端订单ID", 503
            )
        if intent["action"].startswith("OPEN") and observation.state in {
            "live",
            "partially_filled",
        }:
            async with self.jobs.transaction(attempt) as c:
                await self._lock(c)
                now = await self.database.now(c)
                fact = (
                    (
                        await c.execute(
                            sa.select(s.prediction_facts).where(
                                s.prediction_facts.c.id == intent["prediction_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                validation = await c.scalar(
                    sa.select(s.prediction_states.c.body).where(
                        s.prediction_states.c.prediction_id == intent["prediction_id"]
                    )
                )
                closing = await c.scalar(
                    sa.select(s.trade_intents.c.id)
                    .join(
                        s.trade_intent_states,
                        s.trade_intent_states.c.intent_id == s.trade_intents.c.id,
                    )
                    .where(
                        s.trade_intents.c.position_id == UUID(body["position_id"]),
                        s.trade_intents.c.action == "CLOSE",
                        s.trade_intent_states.c.status.not_in(TERMINAL),
                    )
                    .limit(1)
                )
                cancel = (
                    bool(closing)
                    or fact["expires_at"] <= now
                    or validation["lifecycle"] != "ACTIVE"
                )
                if cancel:
                    await self._event(
                        c,
                        attempt,
                        intent,
                        "CANCEL_REMAINDER",
                        {"client_id": body["order"]["client_id"]},
                        now,
                    )
            if cancel:
                try:
                    # Each cancellation is preceded by a fresh confirmed pending order
                    # lookup; it can only cancel this original order's unfilled remainder.
                    observation = await self.transport.cancel(body["order"])
                except Exception:
                    observation = None
                if observation is None:
                    raise DomainError(
                        "CANCEL_RESULT_UNKNOWN", "撤销剩余数量结果未知；继续查询原订单", 503
                    )
        await self._observe(attempt, intent, body, observation)
        if observation.state not in {"filled", "canceled"}:
            raise DomainError("ORDER_PENDING", "订单待成交；后续查询累计成交结果", 503)
        return intent["id"]

    async def _observe(self, attempt, intent, body, observation):
        async with self.jobs.transaction(attempt) as c:
            await self._lock(c)
            await self.predictions._lock_symbol(c, intent["engine_id"], intent["symbol"])
            now = await self.database.now(c)
            _, current = await self._read(c, attempt)
            if current["status"] in TERMINAL:
                return
            body = dict(current["body"])
            qty = Decimal(body["order"]["quantity"])
            old_qty, old_value, old_fee = map(
                Decimal, (body["applied_quantity"], body["applied_notional"], body["applied_fee"])
            )
            if observation.filled < old_qty or observation.filled > qty:
                raise DomainError("ORDER_QUANTITY_CONFLICT", "累计成交数量冲突，需要核对", 503)
            delta = observation.filled - old_qty
            value = observation.filled * observation.average_price
            if not delta and value != old_value:
                raise DomainError(
                    "FILL_PRICE_CONFLICT", "同一成交数量的价格发生修订，需要核对", 503
                )
            if observation.state == "filled" and observation.filled != qty:
                raise DomainError("ORDER_QUANTITY_CONFLICT", "已成交订单的数量不完整", 503)
            row, account = await self._account(c, now)
            position = await self._position(c, intent)
            if delta:
                fill_price = float((value - old_value) / delta)
                if fill_price <= 0:
                    raise DomainError("FILL_PRICE_CONFLICT", "累计成交价格冲突", 503)
                if intent["action"] == "CLOSE" and (
                    not position or position["position_id"] != intent["position_id"]
                ):
                    raise DomainError("POSITION_CHANGED", "成交对应的原持仓已改变", 503)
                prepared = PreparedPrediction(
                    StrategySignal.model_validate(body["prepared"]["signal"]),
                    RiskDecision.model_validate(body["prepared"]["decision"]),
                    body["prepared"]["trailing_stop_pct"],
                    body["prepared"]["trailing_activation_pct"],
                )
                adapter = self._adapter(account)
                result = ExecutionResult(
                    action=prepared.decision.final_action,
                    symbol=intent["symbol"],
                    success=True,
                    fill_price=fill_price,
                    reason="已确认增量成交",
                    metadata={
                        "exchange_contracts": float(delta),
                        "contract_value": float(body["order"]["contract_value"]),
                        "exchange_fee_amount": float(observation.fee - old_fee),
                    },
                )
                if intent["action"] == "CLOSE":
                    result.metadata["exchange_gross_pnl_amount"] = (
                        float(delta)
                        * float(body["order"]["contract_value"])
                        * (fill_price - account.open_position_entry_price[intent["symbol"]])
                        * (1 if account.open_positions[intent["symbol"]] > 0 else -1)
                    )
                before_pnl = account.realized_pnl_amount
                adapter.apply_increment(
                    prepared,
                    result,
                    MarketSnapshot.model_validate(body["market"]),
                    fraction=float(delta / (qty - old_qty))
                    if intent["action"] == "CLOSE"
                    else float(delta / qty),
                )
                if intent["action"] == "CLOSE":
                    realized = account.realized_pnl_amount - before_pnl
                    body["realized"] = float(body.get("realized", 0)) + realized
                await self._save_account(c, row, account, now)
                active = intent["symbol"] in account.open_positions
                position_body = (
                    dict(position["body"])
                    if position and position["status"] == "OPEN"
                    else dict(
                        direction="UP" if intent["action"] == "OPEN_LONG" else "DOWN",
                        structure_prediction_id=str(intent["prediction_id"]),
                        opening_intent_id=str(intent["id"]),
                        opened_at=json_value(now),
                        structure_verified_until=json_value(
                            boundary(now, "1m") + timedelta(minutes=1)
                        ),
                    )
                )
                if intent["action"] == "CLOSE":
                    position_body["realized"] = (
                        position_body.get("realized", 0) + account.realized_pnl_amount - before_pnl
                    )
                position_body.update(
                    contracts=account.open_position_contracts.get(intent["symbol"], 0),
                    account_key=self.account_key,
                )
                version = position["revision"] + 1 if position else 1
                await c.execute(
                    insert(s.trading_positions)
                    .values(
                        engine_id=intent["engine_id"],
                        symbol=intent["symbol"],
                        position_id=UUID(body["position_id"]),
                        revision=version,
                        status="OPEN" if active else "CLOSED",
                        body=json_value(position_body),
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            s.trading_positions.c.engine_id,
                            s.trading_positions.c.symbol,
                        ],
                        set_=dict(
                            position_id=UUID(body["position_id"]),
                            revision=version,
                            status="OPEN" if active else "CLOSED",
                            body=json_value(position_body),
                            updated_at=now,
                        ),
                    )
                )
                await self._event(
                    c,
                    attempt,
                    intent,
                    "FILL_ACCOUNTING",
                    dict(cumulative_quantity=str(observation.filled), events=adapter.audit.events),
                    now,
                )
            if not delta and observation.fee != old_fee:
                fee_delta = float(observation.fee - old_fee)
                if intent["action"] == "CLOSE":
                    account.realized_pnl_amount += fee_delta
                    body["realized"] = float(body.get("realized", 0)) + fee_delta
                    position_body = dict(position["body"])
                    position_body["realized"] = position_body.get("realized", 0) + fee_delta
                    await c.execute(
                        sa.update(s.trading_positions)
                        .where(scope_filter(s.trading_positions, self.engine_id, intent["symbol"]))
                        .values(body=position_body, updated_at=now)
                    )
                else:
                    account.open_position_entry_fee_amount[intent["symbol"]] = (
                        account.open_position_entry_fee_amount.get(intent["symbol"], 0) + fee_delta
                    )
                await self._save_account(c, row, account, now)
                await self._event(
                    c,
                    attempt,
                    intent,
                    "FEE_ADJUSTMENT",
                    {"cumulative_fee": str(observation.fee)},
                    now,
                )
            # Canceled partial closes leave a position, so do not count a completed
            # trade. Accumulate all close orders and fee revisions for that identity.
            if (
                intent["action"] == "CLOSE"
                and intent["symbol"] not in account.open_positions
                and observation.state in {"filled", "canceled"}
                and not body.get("loss_counted")
            ):
                final_position = await self._position(c, intent)
                total = final_position["body"].get("realized", 0)
                account.consecutive_losses = account.consecutive_losses + 1 if total < 0 else 0
                body["loss_counted"] = True
                await self._save_account(c, row, account, now)
            body.update(
                applied_quantity=str(observation.filled),
                applied_notional=str(value),
                applied_fee=str(observation.fee),
                observation=observation.model_dump(mode="json"),
            )
            status = {
                "filled": "FILLED",
                "canceled": "CANCELED",
                "partially_filled": "PARTIAL",
                "live": "WORKING",
            }[observation.state]
            await self._state(c, attempt, intent, status, body, now)
            if status in TERMINAL:
                await self.predictions.queue_parent(c, self.engine_id, intent["symbol"], now)

    async def monitor(self, attempt):
        """Existing stop/target/trailing rules continue independently of forecast expiry."""
        if not self.config.prediction.execution_enabled or self.settings is None:
            return
        if attempt.kind != "PREDICT_VERIFY":
            raise DomainError("FORBIDDEN", "只有确定性验证任务可监控持仓", 403)
        async with self.io_lock:
            if self.settings.policy_loader:
                self.settings.policy_loader()
            await self._reconcile(attempt)
            async with self.jobs.transaction(attempt) as c:
                await self._lock(c)
                now = await self.database.now(c)
                row, account = await self._account(c, now)
                symbol = attempt.payload["symbol"]
                if symbol not in account.open_positions:
                    return
                market = await self._market(c, symbol, now)
                adapter = self._adapter(account)
                close = adapter.monitor(market)
                await self._save_account(c, row, account, now)
                position = await self._position(c, dict(engine_id=self.engine_id, symbol=symbol))
                if not close.should_close or not position or position["status"] != "OPEN":
                    return
                fact = await c.scalar(
                    sa.select(s.prediction_facts.c.body).where(
                        s.prediction_facts.c.id == UUID(position["body"]["structure_prediction_id"])
                    )
                )
                prediction = PublishedPrediction.model_validate(fact["prediction"])
                await self.predictions._intent(
                    c,
                    prediction,
                    "CLOSE",
                    f"risk:{position['position_id']}:{position['revision']}",
                    position,
                    now,
                    details={"reason": close.reason},
                )

    async def _reconcile(self, attempt):
        if self.settings.snapshot_provider is None:
            return
        async with self.jobs.transaction(attempt) as c:
            await self._lock(c)
            now = await self.database.now(c)
            row, account = await self._account(c, now)
            inflight = await c.scalar(
                sa.select(s.trade_intent_states.c.intent_id)
                .where(s.trade_intent_states.c.status.in_(INFLIGHT))
                .limit(1)
            )
            if inflight:
                return  # The order reconciler applies fills before account reconciliation.
            revision = row["revision"]
        snapshot = await self.settings.snapshot_provider()  # read-only external I/O
        async with self.jobs.transaction(attempt) as c:
            await self._lock(c)
            now = await self.database.now(c)
            row, account = await self._account(c, now)
            if row["revision"] != revision:
                raise DomainError("ACCOUNT_CHANGED", "账户核对期间版本改变", 503)
            unexpected = set(snapshot["positions"]) - set(account.open_positions)
            if unexpected:
                raise DomainError("UNJOURNALED_POSITION", "交易所出现账本外持仓，需先核对", 503)
            for symbol in sorted(account.open_positions):
                await self.predictions._lock_symbol(c, self.engine_id, symbol)
                position = await self._position(c, dict(engine_id=self.engine_id, symbol=symbol))
                if not position or position["status"] != "OPEN":
                    raise DomainError("ACCOUNT_RECONCILIATION_REQUIRED", "持仓身份账本不一致", 503)
                intent = (
                    (
                        await c.execute(
                            sa.select(s.trade_intents).where(
                                s.trade_intents.c.id == UUID(position["body"]["opening_intent_id"])
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                exchange = snapshot["positions"].get(symbol)
                body = dict(position["body"])
                if exchange:
                    quantity = exchange["quantity"]
                    key = body.get("exchange_position_key")
                    if (
                        abs(abs(quantity) - account.open_position_contracts[symbol]) > 1e-9
                        or quantity * account.open_positions[symbol] <= 0
                        or (key and key != exchange["position_key"])
                    ):
                        raise DomainError(
                            "POSITION_IDENTITY_CONFLICT", "交易所持仓身份或数量改变，需核对", 503
                        )
                    if not key:
                        body["exchange_position_key"] = exchange["position_key"]
                        await c.execute(
                            sa.update(s.trading_positions)
                            .where(scope_filter(s.trading_positions, self.engine_id, symbol))
                            .values(body=body, updated_at=now)
                        )
                        await self._event(
                            c,
                            attempt,
                            intent,
                            "POSITION_BOUND",
                            dict(position_key=exchange["position_key"]),
                            now,
                        )
                    continue
                key = body.get("exchange_position_key")
                matches = [
                    r
                    for r in snapshot["closed"]
                    if r["symbol"] == symbol
                    and r["price"] > 0
                    and r["direction"]
                    == ("long" if account.open_positions[symbol] > 0 else "short")
                    and (
                        r["position_key"] == key
                        if key
                        else (
                            int(intent["created_at"].timestamp() * 1000) - 60000
                            <= r["created_ms"]
                            <= int(now.timestamp() * 1000)
                            and abs(r["contracts"] - account.open_position_contracts[symbol])
                            <= 1e-9
                        )
                    )
                ]
                if len(matches) != 1:
                    raise DomainError(
                        "EXIT_FILL_UNCONFIRMED",
                        "缺少唯一可绑定的交易所退出事实；不能以标记价伪造结算",
                        503,
                    )
                closed = matches[0]
                adapter = self._adapter(account)
                from trading_core.schemas import FinalAction

                adapter.book._apply_paper_execution(
                    FinalAction.CLOSE,
                    symbol,
                    0,
                    1,
                    closed["price"],
                    True,
                    event_source="exchange_reconcile",
                    timeframe="1h",
                    reason="交易所已确认退出",
                    execution_metadata={
                        "exchange_net_pnl_amount": closed["net_pnl"] - body.get("realized", 0)
                    },
                )
                account.consecutive_losses = (
                    account.consecutive_losses + 1 if closed["net_pnl"] < 0 else 0
                )
                await c.execute(
                    sa.update(s.trading_positions)
                    .where(scope_filter(s.trading_positions, self.engine_id, symbol))
                    .values(
                        status="CLOSED",
                        revision=position["revision"] + 1,
                        body={**body, "exchange_exit": closed},
                        updated_at=now,
                    )
                )
                await self._event(
                    c,
                    attempt,
                    intent,
                    "EXCHANGE_EXIT",
                    dict(evidence=closed, events=adapter.audit.events),
                    now,
                )
                await self.predictions.queue_parent(c, self.engine_id, symbol, now)
            account.equity = snapshot["equity"]
            if row["revision"] == 1:
                row["body"].update(
                    initial_equity=account.equity,
                    peak_equity=account.equity,
                    day_equity=account.equity,
                )
            await self._save_account(c, row, account, now)
