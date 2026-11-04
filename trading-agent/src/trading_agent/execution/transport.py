"""Order transport only. Durable send authorization belongs to ExecutionService."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256

from pydantic import BaseModel, Field, model_validator
from trading_core.execution.live import OKXLiveExecutor
from trading_core.schemas import OrderRequest


def client_id(intent_id):
    return "pd" + sha256(str(intent_id).encode()).hexdigest()[:30]


class OrderObservation(BaseModel):
    state: str  # live / partially_filled / filled / canceled
    filled: Decimal = Field(ge=0)
    average_price: Decimal = Field(ge=0)
    fee: Decimal = Decimal(0)  # signed quote currency, cumulative
    order_id: str = ""

    @model_validator(mode="after")
    def valid(self):
        if self.state not in {"live", "partially_filled", "filled", "canceled"}:
            raise ValueError("Unrecognized exchange order state")
        if self.filled > 0 and self.average_price <= 0:
            raise ValueError("Filled quantity requires a confirmed average price")
        for value in (self.filled, self.average_price, self.fee):
            if not value.is_finite():
                raise ValueError("Non-finite exchange observation")
        return self


class PaperTransport:
    mode = "paper"

    async def build(self, ident, prepared, market, account):
        # Quantity is base units in paper mode. The journal is its exchange ledger;
        # a paper fill is a pure function of the frozen order, including after restart.
        close = prepared.decision.final_action.value == "close"
        qty = account.open_position_contracts.get(market.symbol) if close else None
        qty = (
            qty
            or account.equity
            * prepared.decision.final_position_pct
            * prepared.decision.final_leverage
            / market.close
        )
        return dict(
            client_id=client_id(ident),
            symbol=market.symbol,
            quantity=str(qty),
            contract_value="1",
            price=str(market.close),
            action=prepared.decision.final_action.value,
            sl=prepared.signal.stop_loss_pct,
            tp=prepared.signal.take_profit_pct,
        )

    async def lookup(self, order):
        return OrderObservation(
            state="filled", filled=order["quantity"], average_price=order["price"]
        )

    async def submit(self, order):
        return await self.lookup(order)

    async def cancel(self, order):
        return await self.lookup(order)


class OKXTransport:
    mode = "live"

    def __init__(self, client):
        self.client = client
        self.executor = OKXLiveExecutor(client)

    async def build(self, ident, prepared, market, account):
        inst = self.executor.swap_inst_id(market.symbol)
        instrument = await self.executor._instrument(inst)
        if instrument.get("instId") != inst:
            raise ValueError("Exchange returned an unrelated instrument")
        for key in ("ctVal", "lotSz", "minSz"):
            value = Decimal(str(instrument[key]))
            if not value.is_finite() or value <= 0:
                raise ValueError("Invalid instrument sizing metadata")
        if prepared.decision.final_action.value == "close":
            # Close the bound remaining contracts even when equity-based sizing
            # would now round below minSz. The exchange validates reduce-only size.
            qty = account.open_position_contracts.get(market.symbol)
            if not qty or qty <= 0:
                raise ValueError("Confirmed position contract quantity required")
            order = OrderRequest(
                symbol=market.symbol,
                inst_id=inst,
                side=self.executor._side(prepared.decision.final_action, market.symbol, account),
                order_type="market",
                position_pct=abs(account.open_positions[market.symbol]),
                leverage=account.open_position_leverage.get(market.symbol, 1),
                reduce_only=True,
                client_order_id=client_id(ident),
                td_mode=self.executor.td_mode,
                quantity=qty,
            )
        else:
            order = self.executor._build_order(
                prepared.decision, market, account, inst, instrument, client_id(ident)
            )
        return dict(
            client_id=order.client_order_id,
            symbol=market.symbol,
            quantity=str(order.quantity),
            contract_value=str(instrument["ctVal"]),
            price=str(market.close),
            action=prepared.decision.final_action.value,
            request=order.model_dump(mode="json"),
            sl=prepared.signal.stop_loss_pct,
            tp=prepared.signal.take_profit_pct,
        )

    async def lookup(self, order):
        req = OrderRequest.model_validate(order["request"])
        row = await self.client.get_order(req.inst_id, cl_ord_id=req.client_order_id)
        if row is None:
            return None  # Absence after SENDING never authorizes another submission.
        if row.get("clOrdId") != req.client_order_id or row.get("instId") != req.inst_id:
            raise ValueError("Exchange returned an unrelated order")
        fee = Decimal(str(row.get("fee") or 0))
        if fee and row.get("feeCcy") != "USDT":
            raise ValueError("Unsupported fee currency")
        return OrderObservation(
            state=row["state"],
            filled=row.get("accFillSz") or 0,
            average_price=row.get("avgPx") or 0,
            fee=fee,
            order_id=row.get("ordId", ""),
        )

    async def submit(self, order):
        req = OrderRequest.model_validate(order["request"])
        if not req.reduce_only:
            await self.client.set_leverage(req.inst_id, req.leverage, req.td_mode)
        # Fixed protection travels atomically with the entry request. Software exits
        # retain the existing trailing/range calculations and bound close intents.
        attached = self.executor._attached_exit_orders(
            req, float(order["price"]), order["sl"], order["tp"]
        )
        await self.client.place_order(
            inst_id=req.inst_id,
            side=req.side,
            size=self.executor._format_size(req.quantity),
            client_order_id=req.client_order_id,
            td_mode=req.td_mode,
            order_type=req.order_type,
            reduce_only=req.reduce_only,
            attach_algo_orders=attached,
        )
        return await self.lookup(order)

    async def cancel(self, order):
        req = OrderRequest.model_validate(order["request"])
        await self.client.cancel_order(req.inst_id, cl_ord_id=req.client_order_id)
        return await self.lookup(order)
