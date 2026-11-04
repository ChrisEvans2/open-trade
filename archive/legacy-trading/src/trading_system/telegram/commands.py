from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from trading_core.config import load_app_config
from trading_core.schemas import AccountState, ExecutionMode, RiskMode, RuntimeState


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


class CommandRouter:
    def __init__(
        self,
        state_path: str | Path = "config/runtime_state.yaml",
        paper_account_path: str | Path = "state/paper_account.json",
        log_dir: str | Path = "logs",
        hermes_config_path: str | Path = HERMES_CONFIG_PATH,
        restart_hermes: bool = True,
    ):
        self.state_path = Path(state_path)
        self.paper_account_path = Path(paper_account_path)
        self.log_dir = Path(log_dir)
        self.hermes_config_path = Path(hermes_config_path)
        self.restart_hermes = restart_hermes

    def handle(self, command: str) -> str:
        state = self._load()
        parts = command.strip().split()
        if not parts:
            return "empty command"
        action = parts[0].lower()
        normalized_action = action.lstrip("!")

        if action == "/status" or normalized_action == "status":
            return self._status(state)
        if action in {"/positions", "/position", "/holdings"} or normalized_action in {"positions", "position", "holdings"}:
            return self._positions()
        if action in {"/social", "/news"} or normalized_action in {"social", "news"}:
            limit = self._parse_limit(parts[1] if len(parts) > 1 else None, default=20, maximum=50)
            return self._recent_social(limit)
        if action == "/report":
            return "最近报告请查看 logs 目录或 Telegram 周期推送。"
        if normalized_action == "model" and len(parts) == 2 and parts[1].lower() == "status":
            return self._model_status()
        if normalized_action == "switch" and len(parts) == 3 and parts[1].lower() == "model":
            return self._switch_model(parts[2])
        if (action == "/mode" or normalized_action == "mode") and len(parts) == 2:
            mode = self._parse_mode(parts[1])
            state.mode = mode
            state.emergency = mode == RiskMode.EMERGENCY
            state.global_paused = mode == RiskMode.PAUSED
            self._save(state)
            return f"mode updated: {mode}"
        if (action == "/pause" or normalized_action == "pause") and len(parts) == 2:
            target = parts[1].upper()
            if target == "ALL":
                state.global_paused = True
            else:
                state.paused_symbols.add(self._normalize_symbol(target))
            self._save(state)
            return f"paused: {target}"
        if (action == "/resume" or normalized_action == "resume") and len(parts) == 2:
            target = parts[1].upper()
            if target == "ALL":
                state.global_paused = False
                state.emergency = False
                state.hard_stop_reason = None
                state.hard_stop_at = None
                state.order_failure_count = 0
                if state.mode in {RiskMode.PAUSED, RiskMode.EMERGENCY}:
                    state.mode = RiskMode.NORMAL if state.execution_mode == ExecutionMode.LIVE else RiskMode.PAPER
            else:
                state.paused_symbols.discard(self._normalize_symbol(target))
            self._save(state)
            return f"resumed: {target}"
        if action == "/emergency" or normalized_action == "emergency":
            state.emergency = True
            state.mode = RiskMode.EMERGENCY
            self._save(state)
            return "emergency mode enabled: only close/reduce actions are allowed"
        if (action == "/why" or normalized_action == "why") and len(parts) == 2 and parts[1] == "last_trade":
            return "last trade reason is stored in the latest logs/*/*/risk_decision.json"
        return f"unknown command: {command}"

    def _load(self) -> RuntimeState:
        if not self.state_path.exists():
            return RuntimeState()
        raw = yaml.safe_load(self.state_path.read_text(encoding="utf-8")) or {}
        return RuntimeState.model_validate(raw)

    def _save(self, state: RuntimeState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            yaml.safe_dump(state.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def _model_status(self) -> str:
        config = load_app_config()
        hermes = self._load_hermes_config()
        hermes_model = (hermes.get("model") or {}).get("default", "")
        main = config.agents["main"]
        social = config.agents["social_analyzer"]
        return "\n".join(
            [
                f"Hermes/TG 当前默认模型：{hermes_model}",
                f"主控主模型：{main.model}",
                f"主控备用模型：{main.secondary_model or '(未设置)'}",
                f"社媒分析模型：{social.model}",
            ]
        )

    def _switch_model(self, value: str) -> str:
        model = self._resolve_model(value)
        config = load_app_config()
        allowed = {config.agents["main"].model}
        if config.agents["main"].secondary_model:
            allowed.add(config.agents["main"].secondary_model)
        if model not in allowed:
            return f"拒绝切换：{model} 不在主控主/备用模型配置里。"

        ok, message = self._probe_model(model)
        if not ok:
            return f"拒绝切换：{model} 连通性测试失败：{message}"

        hermes = self._load_hermes_config()
        hermes.setdefault("model", {})["default"] = model
        self._save_hermes_config(hermes)
        if self.restart_hermes:
            self._schedule_hermes_restart()
            return f"已切换主控模型为：{model}\nHermes 将在几秒后自动重启使配置生效。"
        return f"已切换主控模型为：{model}"

    @staticmethod
    def _resolve_model(value: str) -> str:
        return MAIN_MODEL_ALIASES.get(value.strip().lower(), value.strip())

    def _load_hermes_config(self) -> dict:
        if not self.hermes_config_path.exists():
            return {}
        raw = yaml.safe_load(self.hermes_config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{self.hermes_config_path} must contain a YAML mapping")
        return raw

    def _save_hermes_config(self, raw: dict) -> None:
        self.hermes_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.hermes_config_path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @staticmethod
    def _probe_model(model: str) -> tuple[bool, str]:
        config = load_app_config()
        base_url = config.litellm.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {config.litellm.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=30) as client:
                models_response = client.get(f"{base_url}/models", headers=headers)
                models_response.raise_for_status()
                models = {str(item.get("id")) for item in models_response.json().get("data", []) if item.get("id")}
                if model not in models:
                    return False, "LiteLLM 当前没有暴露该模型"
                response = client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply with OK only."}],
                        "max_tokens": 8,
                        "temperature": 0,
                    },
                )
            if response.status_code >= 400:
                return False, f"HTTP {response.status_code}: {response.text.replace(chr(10), ' ')[:300]}"
            return True, "ok"
        except httpx.HTTPError as exc:
            return False, f"{exc.__class__.__name__}: {exc}"

    @staticmethod
    def _schedule_hermes_restart() -> None:
        subprocess.Popen(
            ["bash", "-lc", "sleep 3; systemctl restart hermes-gateway.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _positions(self) -> str:
        if not self.paper_account_path.exists():
            return "当前本地 paper 无持仓记录。"
        account = AccountState.model_validate_json(self.paper_account_path.read_text(encoding="utf-8"))
        if not account.open_positions:
            return "当前本地 paper 无持仓。"
        lines = ["当前本地 paper 持仓："]
        for symbol, signed_margin_pct in sorted(account.open_positions.items()):
            side = "long" if signed_margin_pct > 0 else "short"
            margin_pct = abs(signed_margin_pct)
            leverage = account.open_position_leverage.get(symbol, 1.0)
            entry = account.open_position_entry_price.get(symbol)
            mark = account.open_position_mark_price.get(symbol)
            pnl = account.open_position_unrealized_pnl_pct.get(symbol)
            notional_pct = margin_pct * leverage
            principal = account.open_position_principal.get(symbol, account.equity * margin_pct)
            notional = account.open_position_notional.get(symbol, account.equity * notional_pct)
            pnl_amount = account.open_position_unrealized_pnl_amount.get(symbol, account.equity * pnl) if pnl is not None else None
            entry_text = f", 开仓价 {entry}" if entry else ""
            mark_text = f", 当前价 {mark}" if mark else ""
            pnl_text = f", 浮盈亏 {pnl:.2%} ({self._money(pnl_amount)})" if pnl is not None and pnl_amount is not None else ""
            lines.append(
                f"- {symbol}: {side}, 仓位本金 {self._money(principal)} ({margin_pct:.2%}), "
                f"模拟杠杆 {leverage:.2f}x, 名义仓位 {self._money(notional)} ({notional_pct:.2%})"
                f"{entry_text}{mark_text}{pnl_text}"
            )
        if account.last_mark_update:
            local_time = account.last_mark_update_local or account.last_mark_update.isoformat()
            lines.append(f"最近盯市更新时间：{local_time} {account.last_mark_update_timezone}")
        total_unrealized = account.total_unrealized_pnl_amount or account.equity * account.total_unrealized_pnl_pct
        realized = account.realized_pnl_amount or account.equity * account.realized_pnl_pct
        lines.append(f"总浮盈亏：{account.total_unrealized_pnl_pct:.2%} ({self._money(total_unrealized)})")
        lines.append(f"累计已实现模拟收益率：{account.realized_pnl_pct:.2%} ({self._money(realized)})")
        return "\n".join(lines)

    @staticmethod
    def _money(value: float) -> str:
        return f"{value:,.2f} USDT"

    def _recent_social(self, limit: int) -> str:
        paths = sorted(self.log_dir.glob("*/*/social_raw.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not paths:
            return "目前 logs 中没有 social_raw.jsonl 社媒抓取记录。"

        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                key = (str(item.get("source") or ""), text[:240])
                if key in seen:
                    continue
                seen.add(key)
                item["_parsed_time"] = self._parse_time(item.get("time"))
                rows.append(item)
            if len(rows) >= limit * 4:
                break

        if not rows:
            return "最近的 social_raw.jsonl 存在，但没有可展示的社媒消息。"
        rows.sort(key=lambda item: item["_parsed_time"], reverse=True)
        lines = [f"最近抓取社媒/新闻（去重后前 {min(limit, len(rows))} 条）："]
        for item in rows[:limit]:
            source = item.get("source") or "unknown"
            when = item["_parsed_time"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
            text = " ".join(str(item.get("text") or "").split())
            if len(text) > 180:
                text = text[:177] + "..."
            lines.append(f"- [{when}] {source}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _parse_limit(value: str | None, default: int, maximum: int) -> int:
        if value is None:
            return default
        try:
            return max(1, min(maximum, int(value)))
        except ValueError:
            return default

    @staticmethod
    def _parse_time(value) -> datetime:
        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _status(state: RuntimeState) -> str:
        paused = ", ".join(sorted(state.paused_symbols)) or "-"
        return (
            f"execution_mode={state.execution_mode}, mode={state.mode}, global_paused={state.global_paused}, "
            f"emergency={state.emergency}, paused_symbols={paused}"
        )

    @staticmethod
    def _parse_mode(value: str) -> RiskMode:
        try:
            return RiskMode(value)
        except ValueError as exc:
            raise ValueError(f"unsupported mode: {value}") from exc

    @staticmethod
    def _normalize_symbol(value: str) -> str:
        return value if value.endswith("USDT") else f"{value}USDT"
