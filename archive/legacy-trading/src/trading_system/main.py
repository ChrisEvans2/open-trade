from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from trading_core.config import load_yaml
from trading_system.agents.main_agent import MainControlAgent
from trading_system.agents.workspace import AgentWorkspace, HermesBus
from trading_core.ai.analyzer import SocialAIAnalyzer
from trading_system.ai.advisor_feedback import update_market_advisor_feedback
from trading_core.ai.litellm_client import LiteLLMClient
from trading_system.ai.market_advisor import CodexCliMarketAdvisorClient, MarketAdvisorClient, run_gateway as run_advisor_gateway
from trading_core.config import load_app_config
from trading_core.execution.live import OKXLiveExecutor
from trading_core.execution.paper import PaperExecutor
from trading_core.execution.paper_risk import PaperPositionRiskManager
from trading_system.market_data.collector import MarketDataService, OKXMarketDataService
from trading_core.market_data.okx_client import OKXReadOnlyClient
from trading_system.market_data.live_universe import OKXLiveUniverseSelector
from trading_core.risk.engine import RiskEngine, RiskLimits
from trading_system.scheduler import TradingScheduler
from trading_core.schemas import AccountState, ExecutionMode, RuntimeState
from trading_core.social.filter import SocialFilter
from trading_core.social.monitor import SocialMonitor
from trading_core.social.sources import build_sources
from trading_system.storage.audit import AuditLogger
from trading_system.strategy.trend_strategy import TrendStrategy
from trading_system.strategy.freqtrade_style import EmaRsiAtrConfig, FreqtradeStyleStrategy
from trading_system.telegram.commands import CommandRouter
from trading_system.telegram.reporter import TelegramReporter


from trading_core.policy import build_risk_limits as _build_risk_limits, load_account_state as _load_account_state

HERMES_CONFIG_PATH = Path("/home/operator/.hermes/config.yaml")
MAIN_MODEL_ALIASES = {
    "codex": "gpt-5.5",
    "gpt5.5": "gpt-5.5",
    "gpt-5.5": "gpt-5.5",
    "flash": "vertex-gemini-3.1-flash-lite",
    "flash-lite": "vertex-gemini-3.1-flash-lite",
    "gemini": "vertex-gemini-3.1-flash-lite",
    "pro": "vertex-gemini-3.1-pro-preview",
    "gemini-pro": "vertex-gemini-3.1-pro-preview",
    "3.1-pro": "vertex-gemini-3.1-pro-preview",
    "vertex-gemini-3.1-flash-lite": "vertex-gemini-3.1-flash-lite",
    "vertex-gemini-3.1-pro-preview": "vertex-gemini-3.1-pro-preview",
}


def build_scheduler(use_litellm: bool = False) -> TradingScheduler:
    config = load_app_config()
    client = LiteLLMClient(config.litellm) if use_litellm else None
    market_advisor_client = None
    if config.market_advisor.enabled:
        if config.market_advisor.provider == "codex_cli":
            market_advisor_client = CodexCliMarketAdvisorClient(config.market_advisor, cwd=config.config_dir.parent)
        else:
            market_advisor_client = MarketAdvisorClient(config.market_advisor)
    state_path = config.config_dir / "runtime_state.yaml"
    runtime_state = RuntimeState.model_validate(__import__("yaml").safe_load(state_path.read_text(encoding="utf-8")) or {})
    strategy_config = load_yaml(config.config_dir / "strategy.yaml")
    risk_config = load_yaml(config.config_dir / "risk.yaml")
    social_config = load_yaml(config.config_dir / "social_sources.yaml")
    account_state_path = config.config_dir.parent / "state" / f"{runtime_state.execution_mode.value}_account.json"
    account_state = _load_account_state(account_state_path)
    use_okx = getattr(build_scheduler, "_use_okx", False)
    okx_client = OKXReadOnlyClient(config.okx) if use_okx else None
    if runtime_state.execution_mode == ExecutionMode.LIVE and okx_client is None:
        raise RuntimeError("execution_mode=live requires --market-data okx")
    market_data = OKXMarketDataService(okx_client) if okx_client else MarketDataService()
    strategy = _build_strategy(strategy_config)
    risk_limits = _build_risk_limits(risk_config, runtime_state.mode, runtime_state.execution_mode)
    executor = OKXLiveExecutor(okx_client) if runtime_state.execution_mode == ExecutionMode.LIVE else PaperExecutor()
    return TradingScheduler(
        market_data=market_data,
        social_monitor=SocialMonitor(build_sources(social_config)),
        social_filter=SocialFilter(),
        ai_analyzer=SocialAIAnalyzer(client, config.agents["social_analyzer"].model, config.runtime.ai_failure_policy),
        strategy=strategy,
        risk_engine=RiskEngine(risk_limits),
        paper_risk=PaperPositionRiskManager(risk_limits),
        executor=executor,
        audit_logger=AuditLogger(config.log_dir),
        reporter=TelegramReporter(enabled=config.runtime.report_to_telegram),
        account_state=account_state,
        runtime_state=runtime_state,
        account_state_path=account_state_path,
        runtime_state_path=state_path,
        account_equity_client=okx_client if config.okx.use_private_read else None,
        live_reconcile_client=okx_client if runtime_state.execution_mode == ExecutionMode.LIVE else None,
        risk_limits_loader=lambda state: _build_risk_limits(
            load_yaml(config.config_dir / "risk.yaml"), state.mode, state.execution_mode
        ),
        market_advisor_client=market_advisor_client,
        market_advisor_config=config.market_advisor,
        social_ai_enabled=config.runtime.social_ai_enabled,
    )


def _build_strategy(strategy_config: dict):
    raw = strategy_config.get("freqtrade_style", {})
    if raw.get("enabled", True):
        return FreqtradeStyleStrategy(
            EmaRsiAtrConfig(
                startup_candle_count=int(raw.get("startup_candle_count", 80)),
                ema_fast=int(raw.get("ema_fast", 12)),
                ema_slow=int(raw.get("ema_slow", 26)),
                rsi_period=int(raw.get("rsi_period", 14)),
                atr_period=int(raw.get("atr_period", 14)),
                rsi_long_min=float(raw.get("rsi_long_min", 52)),
                rsi_short_max=float(raw.get("rsi_short_max", 48)),
                atr_min_pct=float(raw.get("atr_min_pct", 0.001)),
                volume_window=int(raw.get("volume_window", 20)),
                min_volume_ratio=float(raw.get("min_volume_ratio", 0.8)),
                adx_period=int(raw.get("adx_period", 14)),
                min_adx=float(raw.get("min_adx", 20)),
                ema_slope_window=int(raw.get("ema_slope_window", 3)),
                min_ema_slope_pct=float(raw.get("min_ema_slope_pct", 0.001)),
                min_ema_separation_pct=float(raw.get("min_ema_separation_pct", 0.001)),
                max_price_extension_atr=float(raw.get("max_price_extension_atr", 2.0)),
                default_position_pct=float(raw.get("default_position_pct", 0.12)),
                regime_filter_enabled=bool(raw.get("regime_filter_enabled", True)),
                min_trend_efficiency=float(raw.get("min_trend_efficiency", 0.35)),
                short_rebound_guard_enabled=bool(raw.get("short_rebound_guard_enabled", True)),
                short_min_trend_efficiency=float(raw.get("short_min_trend_efficiency", 0.45)),
                short_min_regime_confidence=float(raw.get("short_min_regime_confidence", 0.65)),
                range_trading_enabled=bool(raw.get("range_trading_enabled", True)),
                range_position_pct=float(raw.get("range_position_pct", 0.04)),
                range_min_confidence=float(raw.get("range_min_confidence", 0.65)),
                range_wide_min_confidence=float(raw.get("range_wide_min_confidence", 0.72)),
                range_quality_threshold=float(raw.get("range_quality_threshold", 0.65)),
                trend_min_bars_before_entry=int(raw.get("trend_min_bars_before_entry", 2)),
                min_bars_before_entry=int(raw.get("min_bars_before_entry", 3)),
                range_wide_cooldown_bars=int(raw.get("range_wide_cooldown_bars", 3)),
                range_rsi_long_max=float(raw.get("range_rsi_long_max", 38)),
                range_rsi_short_min=float(raw.get("range_rsi_short_min", 62)),
                range_edge_threshold=float(raw.get("range_edge_threshold", 0.22)),
                late_breakout_max_range_quality=float(raw.get("late_breakout_max_range_quality", 0.25)),
                core_breakout_enabled=bool(raw.get("core_breakout_enabled", True)),
                core_breakout_symbols=tuple(str(item).upper() for item in raw.get("core_breakout_symbols", ["BTCUSDT", "ETHUSDT"])),
                core_breakout_position_pct=float(raw.get("core_breakout_position_pct", 0.03)),
                core_breakout_min_volume_ratio=float(raw.get("core_breakout_min_volume_ratio", 1.0)),
                core_breakout_min_adx=float(raw.get("core_breakout_min_adx", 25.0)),
                core_breakout_max_price_extension_atr=float(raw.get("core_breakout_max_price_extension_atr", 3.0)),
            )
        )
    trend = strategy_config.get("trend", {})
    return TrendStrategy(default_position_pct=float(trend.get("default_position_pct", 0.12)))






def _load_enabled_symbols(
    config_dir: Path,
    explicit_symbols: str | None,
    default_timeframe: str,
    execution_mode: ExecutionMode | str = ExecutionMode.PAPER,
) -> list[tuple[str, str]]:
    if explicit_symbols:
        return [(symbol.strip().upper(), default_timeframe) for symbol in explicit_symbols.split(",") if symbol.strip()]
    raw = load_yaml(config_dir / "symbols.yaml")
    result: list[tuple[str, str]] = []
    live_mode = ExecutionMode(execution_mode) == ExecutionMode.LIVE
    for item in raw.get("symbols", []):
        enabled = item.get("live_enabled", False) if live_mode else item.get("enabled", False)
        if enabled:
            result.append((str(item["symbol"]).upper(), str(item.get("timeframe", default_timeframe))))
    return result or [("BTCUSDT", default_timeframe)]


def build_main_agent() -> MainControlAgent:
    config = load_app_config()
    workspaces = {name: AgentWorkspace(agent.workspace) for name, agent in config.agents.items()}
    bus = HermesBus(workspaces)
    return MainControlAgent(
        name=config.agents["main"].name,
        workspace=workspaces["main"],
        bus=bus,
        command_router=CommandRouter(
            config.config_dir / "runtime_state.yaml",
            config.config_dir.parent / "state" / f"{RuntimeState.model_validate(load_yaml(config.config_dir / 'runtime_state.yaml')).execution_mode.value}_account.json",
            config.log_dir,
        ),
    )


async def run_cycle(args: argparse.Namespace) -> None:
    build_scheduler._use_okx = args.market_data == "okx"
    scheduler = build_scheduler(use_litellm=args.use_litellm)
    artifacts = await scheduler.run_cycle(args.symbol, args.timeframe, args.lookback_hours)
    report = TelegramReporter.render_cycle_report(artifacts)
    build_main_agent().forward_report_to_human(report)
    print(report)


async def run_daemon(args: argparse.Namespace) -> None:
    build_scheduler._use_okx = args.market_data == "okx"
    scheduler = build_scheduler(use_litellm=args.use_litellm)
    config = load_app_config()
    runtime_state = RuntimeState.model_validate(load_yaml(config.config_dir / "runtime_state.yaml"))
    static_symbols = _load_enabled_symbols(config.config_dir, args.symbols, args.timeframe, runtime_state.execution_mode)
    universe_config = load_yaml(config.config_dir / "symbols.yaml").get("live_universe", {})
    selector = None
    if (
        runtime_state.execution_mode == ExecutionMode.LIVE
        and not args.symbols
        and universe_config.get("enabled", False)
    ):
        selector = OKXLiveUniverseSelector(
            OKXReadOnlyClient(config.okx), universe_config, config.config_dir.parent / "state" / "live_universe.json"
        )

    async def refresh_symbols() -> list[tuple[str, str]]:
        if selector is None:
            return static_symbols
        try:
            return await selector.select(args.timeframe)
        except Exception as exc:
            print(f"live universe refresh failed: {exc.__class__.__name__}: {exc}; using static core symbols", flush=True)
            return static_symbols

    symbols = await refresh_symbols()
    last_cycle_at: datetime | None = None
    while True:
        started = datetime.now(timezone.utc).isoformat()
        symbol_timeframes = {symbol: timeframe for symbol, timeframe in symbols}
        for symbol in list(scheduler.account_state.open_positions):
            try:
                await scheduler.sync_market_state(symbol, symbol_timeframes.get(symbol, args.timeframe))
            except Exception as exc:
                print(f"[{started}] mark sync failed for {symbol}: {exc.__class__.__name__}: {exc}", flush=True)
            await asyncio.sleep(args.symbol_delay_seconds)

        now = datetime.now(timezone.utc)
        due_for_cycle = last_cycle_at is None or (now - last_cycle_at).total_seconds() >= args.interval_seconds
        if due_for_cycle:
            last_cycle_at = now
            symbols = await refresh_symbols()
            for symbol, timeframe in symbols:
                try:
                    artifacts = await scheduler.run_cycle(symbol, timeframe, args.lookback_hours)
                    report = TelegramReporter.render_cycle_report(artifacts)
                    build_main_agent().forward_report_to_human(report)
                    print(report, flush=True)
                except Exception as exc:
                    print(f"[{started}] cycle failed for {symbol} {timeframe}: {exc.__class__.__name__}: {exc}", flush=True)
                await asyncio.sleep(args.symbol_delay_seconds)
        await asyncio.sleep(args.mark_interval_seconds)


async def run_okx_balance(_: argparse.Namespace) -> None:
    config = load_app_config()
    client = OKXReadOnlyClient(config.okx)
    result = await client.get_balance()
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def run_okx_account(_: argparse.Namespace) -> None:
    config = load_app_config()
    client = OKXReadOnlyClient(config.okx)
    trading = await client.get_balance()
    funding = await client.get_funding_balance()
    data = (trading.get("data") or [{}])[0]
    funding_rows = funding.get("data") or []
    non_zero_funding = [row for row in funding_rows if float(row.get("availBal") or 0) != 0 or float(row.get("bal") or 0) != 0]
    print(f"OKX 真实只读交易账户：totalEq={data.get('totalEq', '')}, details={len(data.get('details', []))}")
    print(f"OKX 真实只读资金账户：非零币种={len(non_zero_funding)}")
    for row in non_zero_funding[:20]:
        print(f"- {row.get('ccy')}: bal={row.get('bal')} availBal={row.get('availBal')} frozenBal={row.get('frozenBal')}")


def _resolve_main_model(value: str) -> str:
    return MAIN_MODEL_ALIASES.get(value.strip().lower(), value.strip())


def _load_hermes_config() -> dict:
    if not HERMES_CONFIG_PATH.exists():
        return {}
    raw = yaml.safe_load(HERMES_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{HERMES_CONFIG_PATH} must contain a YAML mapping")
    return raw


def _save_hermes_config(raw: dict) -> None:
    HERMES_CONFIG_PATH.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


async def _list_litellm_models() -> set[str]:
    config = load_app_config()
    headers = {"Authorization": f"Bearer {config.litellm.api_key}"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{config.litellm.base_url.rstrip('/')}/models", headers=headers)
        response.raise_for_status()
    payload = response.json()
    return {str(item.get("id")) for item in payload.get("data", []) if item.get("id")}


async def _probe_litellm_model(model: str) -> tuple[bool, str]:
    config = load_app_config()
    headers = {"Authorization": f"Bearer {config.litellm.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{config.litellm.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            detail = response.text.replace("\n", " ")[:500]
            return False, f"HTTP {response.status_code}: {detail}"
        return True, "ok"
    except httpx.HTTPError as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


async def run_model_status(_: argparse.Namespace) -> None:
    config = load_app_config()
    hermes = _load_hermes_config()
    hermes_model = (hermes.get("model") or {}).get("default", "")
    hermes_provider = (hermes.get("model") or {}).get("provider", "")
    hermes_base_url = (hermes.get("model") or {}).get("base_url", "")
    main = config.agents["main"]
    social = config.agents["social_analyzer"]
    print(f"Hermes/TG 当前默认模型: {hermes_model}")
    print(f"Hermes/TG 当前 provider: {hermes_provider or '(未设置)'}")
    if hermes_base_url:
        print(f"Hermes/TG 当前 base_url: {hermes_base_url}")
    print(f"交易主控配置主模型: {main.model}")
    print(f"交易主控配置备用模型: {main.secondary_model or '(未设置)'}")
    print(f"社媒分析 agent 模型: {social.model}")
    try:
        models = await _list_litellm_models()
    except Exception as exc:
        print(f"LiteLLM 模型列表读取失败: {exc.__class__.__name__}: {exc}")
        return
    litellm_models = [social.model, main.secondary_model]
    if hermes_provider not in {"openai-codex", "codex"}:
        litellm_models.insert(0, main.model)
    else:
        print(f"- {main.model}: 走 Hermes openai-codex 订阅通道，不检查 LiteLLM 暴露列表")
    for model in litellm_models:
        if model:
            print(f"- {model}: {'已暴露' if model in models else '未暴露'}")
    if main.secondary_model:
        ok, message = await _probe_litellm_model(main.secondary_model)
        print(f"备用模型连通性: {'可用' if ok else '不可用'} ({message})")


async def run_switch_main_model(args: argparse.Namespace) -> None:
    model = _resolve_main_model(args.model)
    config = load_app_config()
    allowed = {config.agents["main"].model}
    if config.agents["main"].secondary_model:
        allowed.add(config.agents["main"].secondary_model)
    if model not in allowed and not args.force:
        print(f"拒绝切换：{model} 不在 app.yaml 的主控主/备用模型里。可用 --force 强制写入。")
        return

    if not args.force:
        models = await _list_litellm_models()
        if model not in models:
            print(f"拒绝切换：LiteLLM 当前没有暴露 {model}。")
            return
        ok, message = await _probe_litellm_model(model)
        if not ok:
            print(f"拒绝切换：{model} 连通性测试失败：{message}")
            print("默认模型保持不变，主控 TG agent 不会被切到不可用模型。")
            return

    hermes = _load_hermes_config()
    hermes.setdefault("model", {})["default"] = model
    _save_hermes_config(hermes)
    if not args.no_restart:
        subprocess.run(["systemctl", "restart", "hermes-gateway.service"], check=True)
    print(f"已将 Hermes/TG 主控默认模型切换为: {model}")


def run_status(_: argparse.Namespace) -> None:
    print(build_main_agent().receive_human_command("/status"))


def run_positions(_: argparse.Namespace) -> None:
    print(build_main_agent().receive_human_command("/positions"))


def run_command(args: argparse.Namespace) -> None:
    result = build_main_agent().receive_human_command(args.command)
    print(result)


def run_advisor_feedback(args: argparse.Namespace) -> None:
    result = update_market_advisor_feedback(log_dir=args.log_dir, state_dir=args.state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="trade-agent")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    cycle = subparsers.add_parser("cycle")
    cycle.add_argument("--symbol", default="BTCUSDT")
    cycle.add_argument("--timeframe", default="1h")
    cycle.add_argument("--lookback-hours", type=int, default=1)
    cycle.add_argument("--use-litellm", action="store_true")
    cycle.add_argument("--market-data", choices=["fixture", "okx"], default="okx")
    cycle.set_defaults(func=lambda args: asyncio.run(run_cycle(args)))

    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--symbol", default="BTCUSDT")
    daemon.add_argument("--symbols", default=None, help="Comma-separated symbols. Defaults to enabled config/symbols.yaml entries.")
    daemon.add_argument("--timeframe", default="1h")
    daemon.add_argument("--lookback-hours", type=int, default=1)
    daemon.add_argument("--interval-seconds", type=int, default=3600)
    daemon.add_argument("--mark-interval-seconds", type=int, default=300)
    daemon.add_argument("--symbol-delay-seconds", type=int, default=5)
    daemon.add_argument("--use-litellm", action="store_true")
    daemon.add_argument("--market-data", choices=["fixture", "okx"], default="okx")
    daemon.set_defaults(func=lambda args: asyncio.run(run_daemon(args)))

    okx_balance = subparsers.add_parser("okx-balance")
    okx_balance.set_defaults(func=lambda args: asyncio.run(run_okx_balance(args)))

    okx_account = subparsers.add_parser("okx-account")
    okx_account.set_defaults(func=lambda args: asyncio.run(run_okx_account(args)))

    advisor_gateway = subparsers.add_parser("advisor-gateway")
    advisor_gateway.add_argument("--host", default="127.0.0.1")
    advisor_gateway.add_argument("--port", type=int, default=8765)
    advisor_gateway.set_defaults(func=run_advisor_gateway)

    advisor_feedback = subparsers.add_parser("advisor-feedback")
    advisor_feedback.add_argument("--log-dir", default="/srv/open-trade-deployment/logs")
    advisor_feedback.add_argument("--state-dir", default="/srv/open-trade-deployment/state")
    advisor_feedback.set_defaults(func=run_advisor_feedback)

    model_status = subparsers.add_parser("model-status")
    model_status.set_defaults(func=lambda args: asyncio.run(run_model_status(args)))

    switch_model = subparsers.add_parser("switch-main-model")
    switch_model.add_argument("model", help="flash, pro, or a full LiteLLM model id.")
    switch_model.add_argument("--force", action="store_true", help="Write the model even if the preflight check fails.")
    switch_model.add_argument("--no-restart", action="store_true", help="Do not restart hermes-gateway.service after writing config.")
    switch_model.set_defaults(func=lambda args: asyncio.run(run_switch_main_model(args)))

    status = subparsers.add_parser("status")
    status.set_defaults(func=run_status)

    positions = subparsers.add_parser("positions")
    positions.set_defaults(func=run_positions)

    command = subparsers.add_parser("command")
    command.add_argument("command")
    command.set_defaults(func=run_command)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
