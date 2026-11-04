from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from trading_core.market_data.okx_client import OKXReadOnlyClient
from trading_core.schemas import (
    AccountState,
    ExecutionResult,
    FinalAction,
    MarketSnapshot,
    OrderRequest,
    RiskDecision,
)


class OKXLiveExecutor:
    def __init__(self, client: OKXReadOnlyClient, td_mode: str = "isolated"):
        self.client = client
        self.td_mode = td_mode
        self.executed_ids: set[str] = set()

    async def execute(
        self,
        decision: RiskDecision,
        market: MarketSnapshot,
        account: AccountState | None = None,
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
        trailing_stop_pct: float | None = None,
        trailing_activation_pct: float | None = None,
    ) -> ExecutionResult:
        if not decision.allow_trade or decision.final_action == FinalAction.HOLD:
            return ExecutionResult(
                action=FinalAction.HOLD,
                symbol=market.symbol,
                success=True,
                reason=decision.reason,
            )

        account = account or AccountState()
        inst_id = self.swap_inst_id(market.symbol)
        client_order_id = self._client_order_id(decision, market)
        if client_order_id in self.executed_ids:
            return ExecutionResult(
                action=FinalAction.HOLD,
                symbol=market.symbol,
                success=False,
                reason="重复 client_order_id，实盘执行已拒绝。",
                metadata={"client_order_id": client_order_id},
            )

        try:
            instrument = await self._instrument(inst_id)
            order = self._build_order(
                decision, market, account, inst_id, instrument, client_order_id
            )
            if decision.final_action in {FinalAction.OPEN_LONG, FinalAction.OPEN_SHORT}:
                await self.client.set_leverage(inst_id, order.leverage, self.td_mode)
            response = await self.client.place_order(
                inst_id=inst_id,
                side=order.side,
                size=self._format_size(order.quantity),
                client_order_id=order.client_order_id,
                td_mode=self.td_mode,
                order_type=order.order_type,
                reduce_only=order.reduce_only,
            )
            order_row = (response.get("data") or [{}])[0]
            exchange_order = await self.client.get_order(
                inst_id,
                ord_id=order_row.get("ordId"),
                cl_ord_id=client_order_id,
            )
            fills = await self.client.get_fills(inst_id, ord_id=order_row.get("ordId"))
            actual_fill_price = (
                self._positive_float(exchange_order.get("avgPx"))
                if exchange_order
                else None
            )
            exit_base_price = actual_fill_price or market.close
            fixed_exit_response = None
            if not order.reduce_only and stop_loss_pct and take_profit_pct:
                fixed_exit_response = await self.client.place_algo_order(
                    self._fixed_exit_order(
                        order, exit_base_price, stop_loss_pct, take_profit_pct
                    )
                )
            trailing_response = None
            if not order.reduce_only and trailing_stop_pct and trailing_activation_pct:
                trailing_response = await self.client.place_algo_order(
                    self._trailing_exit_order(
                        order,
                        exit_base_price,
                        trailing_stop_pct,
                        trailing_activation_pct,
                    )
                )
        except Exception as exc:
            error_response = getattr(exc, "response", None)
            return ExecutionResult(
                action=decision.final_action,
                symbol=market.symbol,
                success=False,
                reason=f"OKX live order failed: {exc.__class__.__name__}: {exc}",
                metadata={
                    "inst_id": inst_id,
                    "client_order_id": client_order_id,
                    "okx_error_response": error_response,
                },
            )

        self.executed_ids.add(client_order_id)
        actual_contracts = (
            self._positive_float(exchange_order.get("accFillSz"))
            if exchange_order
            else None
        )
        fill_fee = sum(self._optional_float(item.get("fee")) for item in fills)
        fill_pnl = sum(self._optional_float(item.get("fillPnl")) for item in fills)
        return ExecutionResult(
            action=decision.final_action,
            symbol=market.symbol,
            success=True,
            order=order,
            fill_price=actual_fill_price or market.close,
            reason="OKX live order submitted",
            metadata={
                "okx_response": response,
                "fixed_exit_response": fixed_exit_response,
                "trailing_response": trailing_response,
                "inst_id": inst_id,
                "client_order_id": client_order_id,
                "exchange_order_id": order_row.get("ordId"),
                "exchange_contracts": actual_contracts or order.quantity,
                "contract_value": self._optional_float(instrument.get("ctVal")),
                "exchange_fee_amount": fill_fee,
                "exchange_gross_pnl_amount": fill_pnl,
            },
        )

    @staticmethod
    def _optional_float(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _positive_float(value) -> float | None:
        parsed = OKXLiveExecutor._optional_float(value)
        return parsed if parsed > 0 else None

    @staticmethod
    def _trailing_exit_order(
        order: OrderRequest,
        entry_price: float,
        trailing_stop_pct: float,
        trailing_activation_pct: float,
    ) -> dict[str, str | bool]:
        active_price = (
            entry_price * (1 + trailing_activation_pct)
            if order.side == "buy"
            else entry_price * (1 - trailing_activation_pct)
        )
        return {
            "instId": order.inst_id or "",
            "tdMode": order.td_mode or "isolated",
            "side": "sell" if order.side == "buy" else "buy",
            "ordType": "move_order_stop",
            "sz": OKXLiveExecutor._format_size(order.quantity),
            "activePx": f"{active_price:.8f}",
            "callbackRatio": f"{trailing_stop_pct:.8f}",
            "reduceOnly": True,
            "closeOnTrigger": True,
        }

    @staticmethod
    def _fixed_exit_order(
        order: OrderRequest,
        entry_price: float,
        stop_loss_pct: float,
        take_profit_pct: float,
    ) -> dict[str, str | bool]:
        exits = OKXLiveExecutor._attached_exit_orders(
            order, entry_price, stop_loss_pct, take_profit_pct
        )
        if not exits:
            raise ValueError("missing fixed exit parameters")
        fixed_exit = exits[0]
        return {
            "instId": order.inst_id or "",
            "tdMode": order.td_mode or "isolated",
            "side": "sell" if order.side == "buy" else "buy",
            "ordType": "oco",
            "sz": OKXLiveExecutor._format_size(order.quantity),
            "reduceOnly": True,
            "closeOnTrigger": True,
            **{
                key: value
                for key, value in fixed_exit.items()
                if key != "attachAlgoClOrdId"
            },
        }

    @staticmethod
    def _attached_exit_orders(
        order: OrderRequest,
        entry_price: float,
        stop_loss_pct: float | None,
        take_profit_pct: float | None,
    ) -> list[dict[str, str]] | None:
        if order.reduce_only or stop_loss_pct is None or take_profit_pct is None:
            return None
        if order.side == "buy":
            stop_price = entry_price * (1 - stop_loss_pct)
            take_price = entry_price * (1 + take_profit_pct)
        else:
            stop_price = entry_price * (1 + stop_loss_pct)
            take_price = entry_price * (1 - take_profit_pct)
        return [
            {
                "attachAlgoClOrdId": f"{order.client_order_id}x"[:32],
                "tpTriggerPx": f"{take_price:.8f}",
                "tpOrdPx": "-1",
                "tpTriggerPxType": "mark",
                "slTriggerPx": f"{stop_price:.8f}",
                "slOrdPx": "-1",
                "slTriggerPxType": "mark",
            }
        ]

    def _build_order(
        self,
        decision: RiskDecision,
        market: MarketSnapshot,
        account: AccountState,
        inst_id: str,
        instrument: dict,
        client_order_id: str,
    ) -> OrderRequest:
        reduce_only = decision.final_action == FinalAction.CLOSE
        side = self._side(decision.final_action, market.symbol, account)
        position_pct = decision.final_position_pct
        leverage = decision.final_leverage
        if reduce_only:
            signed_margin = account.open_positions.get(market.symbol)
            if signed_margin is None:
                raise ValueError(f"no local live position to close for {market.symbol}")
            position_pct = abs(signed_margin)
            leverage = account.open_position_leverage.get(
                market.symbol, max(1.0, decision.final_leverage)
            )
        quantity = self._order_quantity(
            account.equity, position_pct, leverage, market.close, instrument
        )
        return OrderRequest(
            symbol=market.symbol,
            inst_id=inst_id,
            side=side,
            order_type="market",
            position_pct=position_pct,
            leverage=leverage,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            td_mode=self.td_mode,
            quantity=quantity,
        )

    @staticmethod
    def _side(action: FinalAction, symbol: str, account: AccountState) -> str:
        if action == FinalAction.OPEN_LONG:
            return "buy"
        if action == FinalAction.OPEN_SHORT:
            return "sell"
        if action == FinalAction.CLOSE:
            signed_margin = account.open_positions.get(symbol)
            if signed_margin is None:
                raise ValueError(f"no local live position side for {symbol}")
            return "sell" if signed_margin > 0 else "buy"
        raise ValueError(f"unsupported live action: {action}")

    @staticmethod
    def _order_quantity(
        equity: float,
        position_pct: float,
        leverage: float,
        price: float,
        instrument: dict,
    ) -> float:
        if equity <= 0 or position_pct <= 0 or leverage <= 0 or price <= 0:
            raise ValueError("invalid live sizing inputs")
        ct_val = Decimal(str(instrument.get("ctVal") or "1"))
        lot_size = Decimal(str(instrument.get("lotSz") or "1"))
        min_size = Decimal(str(instrument.get("minSz") or lot_size))
        raw_contracts = (
            Decimal(str(equity)) * Decimal(str(position_pct)) * Decimal(str(leverage))
        )
        raw_contracts = raw_contracts / (Decimal(str(price)) * ct_val)
        contracts = (raw_contracts / lot_size).to_integral_value(
            rounding=ROUND_DOWN
        ) * lot_size
        if contracts < min_size:
            raise ValueError(f"order size {contracts} below OKX minSz {min_size}")
        return float(contracts)

    async def _instrument(self, inst_id: str) -> dict:
        rows = await self.client.get_instruments("SWAP", inst_id)
        if not rows:
            raise ValueError(f"OKX instrument not found: {inst_id}")
        return rows[0]

    @staticmethod
    def _format_size(quantity: float | None) -> str:
        if quantity is None:
            raise ValueError("missing order quantity")
        return format(Decimal(str(quantity)).normalize(), "f")

    @staticmethod
    def _client_order_id(decision: RiskDecision, market: MarketSnapshot) -> str:
        stamp = market.timestamp.strftime("%Y%m%d%H%M%S")
        action = {
            FinalAction.OPEN_LONG: "ol",
            FinalAction.OPEN_SHORT: "os",
            FinalAction.CLOSE: "cl",
            FinalAction.HOLD: "hd",
        }[decision.final_action]
        return f"lv{stamp}{market.symbol}{action}"[:32]

    @staticmethod
    def swap_inst_id(symbol: str) -> str:
        if symbol.endswith("-SWAP"):
            return symbol
        if "-" in symbol:
            return f"{symbol}-SWAP"
        if symbol.endswith("USDT"):
            return f"{symbol[:-4]}-USDT-SWAP"
        return symbol
