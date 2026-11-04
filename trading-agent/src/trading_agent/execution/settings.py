"""Load the existing account/risk policy only when execution is explicitly enabled."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path

from trading_core.ai.analyzer import SocialAIAnalyzer
from trading_core.ai.litellm_client import LiteLLMClient
from trading_core.config import load_app_config, load_yaml
from trading_core.market_data.okx_client import OKXReadOnlyClient
from trading_core.policy import (
    build_risk_limits,
    load_account_state,
    load_trade_parameters,
)
from trading_core.schemas import AccountState, RuntimeState
from trading_core.social.filter import SocialFilter
from trading_core.social.monitor import SocialMonitor
from trading_core.social.sources import build_sources

from trading_agent.domain.errors import DomainError
from trading_agent.execution.transport import OKXTransport, PaperTransport


@dataclass
class ExecutionSettings:
    account_key: str
    limits: object
    strategy: object
    runtime: RuntimeState
    initial_account: AccountState
    social_provider: object
    refresh_provider: object = None
    snapshot_provider: object = None
    policy_loader: object = None

    async def social(self, symbol, market):
        return await self.social_provider(symbol, market)

    async def refresh(self, account):
        return await self.refresh_provider(account) if self.refresh_provider else account


def require_trade_parameters(raw):
    if not raw.get("freqtrade_style", {}).get("enabled", True):
        raise DomainError("RISK_ADAPTER_UNSUPPORTED", "预测执行需要现有ATR与区间风险参数", 503)
    return load_trade_parameters(raw)


def load_execution(config):
    root = Path(config.prediction.legacy_config_dir).resolve()
    if not all(
        (root / name).is_file()
        for name in ("app.yaml", "risk.yaml", "strategy.yaml", "runtime_state.yaml")
    ):
        raise DomainError(
            "LEGACY_RISK_CONFIG_MISSING", "必须提供现有交易配置目录，禁止使用空配置替代", 503
        )
    app = load_app_config(root)
    runtime = RuntimeState.model_validate(load_yaml(root / "runtime_state.yaml"))
    limits = build_risk_limits(load_yaml(root / "risk.yaml"), runtime.mode, runtime.execution_mode)
    strategy = require_trade_parameters(load_yaml(root / "strategy.yaml"))
    account = load_account_state(
        root.parent / "state" / f"{runtime.execution_mode.value}_account.json"
    )
    client = OKXReadOnlyClient(app.okx) if runtime.execution_mode.value == "live" else None
    if client and not all((app.okx.api_key, app.okx.api_secret, app.okx.api_passphrase)):
        raise DomainError("EXCHANGE_CREDENTIALS_MISSING", "实盘执行凭证未配置", 503)
    source_config = load_yaml(root / "social_sources.yaml")
    sources = build_sources(source_config)
    # Reuse source filters and analyzer, but do not combine its direction with the parent.
    monitor, filters = SocialMonitor(sources), SocialFilter()
    analyzer = SocialAIAnalyzer(
        LiteLLMClient(app.litellm) if app.runtime.social_ai_enabled else None,
        app.agents["social_analyzer"].model,
        app.runtime.ai_failure_policy,
    )

    async def social(symbol, market):
        if not app.runtime.social_ai_enabled:
            return analyzer._no_recent_data(symbol.removesuffix("USDT"))
        # Source failures must not look like a successfully empty news window.
        results = await asyncio.gather(
            *(
                source.fetch(symbol=symbol, lookback_hours=app.runtime.social_lookback_hours)
                for source in monitor.sources
            ),
            return_exceptions=True,
        )
        if any(isinstance(r, Exception) for r in results):
            return analyzer._fallback(
                symbol.removesuffix("USDT"),
                "社媒来源暂不可用；沿用原降级规则",
                status="model_error",
                error="SOURCE_UNAVAILABLE",
            )
        items = filters.filter_items(
            [item for result in results for item in result],
            symbol=symbol,
            lookback_hours=app.runtime.social_lookback_hours,
        )
        return await analyzer.analyze(symbol, items, market)

    async def refresh(account):
        equity, rows = await asyncio.gather(
            client.get_account_equity(), client.get_positions("SWAP")
        )
        if equity is None or not math.isfinite(equity) or equity <= 0:
            raise DomainError("ACCOUNT_UNAVAILABLE", "无法确认真实账户权益", 503)
        positions = {}
        for row in rows:
            size = float(row.get("pos") or 0)
            if not math.isfinite(size):
                raise DomainError("POSITION_INVALID", "交易所持仓数量无效", 503)
            if not size:
                continue
            if row.get("posSide") not in ("net", None, ""):
                raise DomainError("POSITION_MODE_UNSUPPORTED", "执行入口要求净持仓模式", 409)
            inst = row["instId"]
            symbol = inst.replace("-USDT-SWAP", "USDT")
            if symbol in positions:
                raise DomainError("POSITION_AMBIGUOUS", "账户持仓模式不唯一", 409)
            positions[symbol] = size
        if set(positions) != set(account.open_positions) or any(
            abs(abs(size) - account.open_position_contracts.get(symbol, 0)) > 1e-9
            or size * account.open_positions[symbol] <= 0
            for symbol, size in positions.items()
        ):
            raise DomainError(
                "ACCOUNT_RECONCILIATION_REQUIRED",
                "交易所持仓与已确认账本不一致，暂停新提交并等待核对",
                503,
            )
        account.equity = equity
        return account

    async def snapshot():
        equity, rows, history, pending = await asyncio.gather(
            client.get_account_equity(),
            client.get_positions("SWAP"),
            client.get_positions_history("SWAP", limit=100),
            client.get_pending_orders("SWAP"),
        )
        if equity is None or not math.isfinite(equity) or equity <= 0:
            raise DomainError("ACCOUNT_UNAVAILABLE", "无法确认账户权益", 503)
        if pending:
            raise DomainError("UNJOURNALED_ORDER", "存在未结订单，先完成订单查询", 503)
        positions = {}
        for r in rows:
            quantity = float(r.get("pos") or 0)
            if not math.isfinite(quantity):
                raise DomainError("POSITION_INVALID", "交易所持仓数量无效", 503)
            if not quantity:
                continue
            if r.get("posSide") not in ("net", "", None):
                raise DomainError("POSITION_MODE_UNSUPPORTED", "执行入口要求净持仓模式", 409)
            symbol = r["instId"].replace("-USDT-SWAP", "USDT")
            if symbol in positions:
                raise DomainError("POSITION_AMBIGUOUS", "账户持仓模式不唯一", 409)
            positions[symbol] = {
                "quantity": quantity,
                "position_key": r.get("posId"),
                "mark_price": float(r.get("markPx") or 0),
            }
        closed = []
        for r in history:
            if float(r.get("closeTotalPos") or 0) <= 0 or float(r.get("openMaxPos") or 0) != float(
                r.get("closeTotalPos") or 0
            ):
                continue
            closed.append(
                dict(
                    symbol=r["instId"].replace("-USDT-SWAP", "USDT"),
                    position_key=r.get("posId"),
                    direction=r.get("direction"),
                    contracts=float(r["closeTotalPos"]),
                    price=float(r.get("closeAvgPx") or 0),
                    net_pnl=float(r["realizedPnl"]),
                    created_ms=int(r.get("cTime") or 0),
                    updated_ms=int(r.get("uTime") or 0),
                )
            )
        if any(
            not math.isfinite(p["mark_price"]) or p["mark_price"] <= 0 for p in positions.values()
        ):
            raise DomainError("POSITION_INVALID", "交易所持仓价格无效", 503)
        if any(
            not all(math.isfinite(r[k]) for k in ("contracts", "price", "net_pnl")) for r in closed
        ):
            raise DomainError("EXIT_INVALID", "交易所退出事实包含无效数字", 503)
        return {"equity": equity, "positions": positions, "closed": closed}

    settings = ExecutionSettings(
        config.prediction.account_key,
        limits,
        strategy,
        runtime,
        account,
        social,
        refresh if client else None,
        snapshot if client else None,
    )

    def reload_policy():
        current = RuntimeState.model_validate(load_yaml(root / "runtime_state.yaml"))
        if current.execution_mode != settings.runtime.execution_mode:
            raise DomainError("EXECUTION_MODE_CHANGED", "执行模式变化需要重新校验并重启入口", 503)
        settings.runtime = current
        settings.limits = build_risk_limits(
            load_yaml(root / "risk.yaml"), current.mode, current.execution_mode
        )
        settings.strategy = require_trade_parameters(load_yaml(root / "strategy.yaml"))

    settings.policy_loader = reload_policy
    return settings, OKXTransport(client) if client else PaperTransport()
