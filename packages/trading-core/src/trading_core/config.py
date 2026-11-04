from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class LiteLLMConfig(BaseModel):
    base_url: str = "http://127.0.0.1:4000/v1"
    api_key: str = "dummy"
    timeout_seconds: int = 60


class MarketAdvisorTimeframeConfig(BaseModel):
    name: str
    timeframe: str
    advisor_type: str
    max_candles: int = 80


class MarketAdvisorConfig(BaseModel):
    enabled: bool = False
    provider: str = "http_gateway"
    live_influence_enabled: bool = False
    codex_command: str = "codex"
    codex_model: str = "gpt-5.5"
    codex_timeout_seconds: int = 180
    trigger_cooldown_minutes: int = 120
    max_triggers_per_symbol_5h: int = 2
    max_triggers_global_5h: int = 12
    endpoint: str = "http://127.0.0.1:8765/v1/judge"
    timeout_seconds: int = 3
    failure_policy: str = "strategy_fallback"
    mode: str = "multi_timeframe_early_candidate_confirmed_by_code"
    early_candidate_live_enabled: bool = True
    advisors: list[MarketAdvisorTimeframeConfig] = Field(
        default_factory=lambda: [
            MarketAdvisorTimeframeConfig(
                name="advisor_12h",
                timeframe="12h",
                advisor_type="macro_regime",
                max_candles=36,
            ),
            MarketAdvisorTimeframeConfig(
                name="advisor_4h",
                timeframe="4h",
                advisor_type="structure_bias",
                max_candles=40,
            ),
            MarketAdvisorTimeframeConfig(
                name="advisor_1h",
                timeframe="1h",
                advisor_type="early_candidate",
                max_candles=40,
            ),
            MarketAdvisorTimeframeConfig(
                name="advisor_15m",
                timeframe="15m",
                advisor_type="execution_quality",
                max_candles=32,
            ),
        ]
    )
    min_model_confidence: float = 0.62
    breakout_lookback_bars: int = 20
    breakout_buffer_atr: float = 0.15
    min_volume_ratio: float = 1.25
    sizing_mode: str = "risk_based"
    early_candidate_risk_pct: float = 0.0025
    early_candidate_max_leverage: float = 3.0
    max_meta_risk_budget_multiplier: float = 0.5
    max_early_margin_pct: float = 0.02
    min_stop_loss_atr: float = 0.8
    max_stop_loss_atr: float = 2.0
    max_stop_loss_pct: float = 0.012
    max_extension_atr_from_structure: float = 0.8
    min_reward_risk_ratio: float = 1.5
    btc_eth_confirmation_required: bool = True
    block_regimes: list[str] = Field(
        default_factory=lambda: ["range", "choppy_high_vol", "transition", "shock"]
    )
    cooldown_bars_after_loss: int = 4
    no_reverse_after_stop_bars: int = 3
    max_early_trades_per_symbol_per_day: int = 2
    require_risk_engine_approval: bool = True


class AgentConfig(BaseModel):
    name: str
    model: str
    secondary_model: str | None = None
    workspace: Path


class RuntimeConfig(BaseModel):
    mode: str = "paper"
    execution_mode: str = "paper"
    social_ai_enabled: bool = True
    social_lookback_hours: int = 1
    ai_failure_policy: str = "block_new_entries"
    report_to_telegram: bool = False


class OKXConfig(BaseModel):
    name: str = "okx"
    base_url: str = "https://www.okx.com"
    user_agent: str = "open-trade/0.1 (OKX REST client)"
    simulated_trading: bool = False
    use_private_read: bool = False
    default_inst_type: str = "SWAP"
    trust_env_proxy: bool = False
    max_read_retries: int = 2
    retry_backoff_seconds: float = 1.0
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None


class AppConfig(BaseModel):
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    market_advisor: MarketAdvisorConfig = Field(default_factory=MarketAdvisorConfig)
    agents: dict[str, AgentConfig]
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    okx: OKXConfig = Field(default_factory=OKXConfig)
    config_dir: Path = Path("config")
    log_dir: Path = Path("logs")


def load_yaml(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_app_config(config_dir: str | Path | None = None) -> AppConfig:
    load_dotenv()
    base_dir = _resolve_config_dir(config_dir)
    project_root = base_dir.parent
    raw = load_yaml(base_dir / "app.yaml")
    exchange_raw = load_yaml(base_dir / "exchange.yaml").get("exchange", {})

    raw.setdefault("agents", {})
    raw["agents"].setdefault(
        "main",
        {
            "name": "main_agent",
            "model": os.getenv("MAIN_AGENT_MODEL", "vertex-gemini-3.1-flash-lite"),
            "workspace": Path(
                os.getenv("TRADING_WORKSPACE_DIR", "workspaces/trading_workspace")
            )
            / "main_agent",
        },
    )
    raw["agents"].setdefault(
        "social_analyzer",
        {
            "name": "social_ai_analyzer",
            "model": os.getenv(
                "SOCIAL_ANALYZER_MODEL", "vertex-gemini-3.1-pro-preview"
            ),
            "workspace": Path(
                os.getenv("TRADING_WORKSPACE_DIR", "workspaces/trading_workspace")
            )
            / "social_ai_analyzer",
        },
    )
    for agent in raw["agents"].values():
        workspace = Path(agent["workspace"])
        if not workspace.is_absolute():
            agent["workspace"] = project_root / workspace

    litellm = raw.setdefault("litellm", {})
    litellm["base_url"] = os.getenv(
        "LITELLM_BASE_URL", litellm.get("base_url", "http://127.0.0.1:4000/v1")
    )
    litellm["api_key"] = os.getenv("LITELLM_API_KEY", litellm.get("api_key", "dummy"))

    advisor = raw.setdefault("market_advisor", {})
    if os.getenv("MARKET_ADVISOR_ENABLED") is not None:
        advisor["enabled"] = os.getenv("MARKET_ADVISOR_ENABLED", "").lower() in {
            "1",
            "true",
            "yes",
        }
    if os.getenv("MARKET_ADVISOR_PROVIDER") is not None:
        advisor["provider"] = os.getenv("MARKET_ADVISOR_PROVIDER", "http_gateway")
    if os.getenv("MARKET_ADVISOR_LIVE_INFLUENCE_ENABLED") is not None:
        advisor["live_influence_enabled"] = os.getenv(
            "MARKET_ADVISOR_LIVE_INFLUENCE_ENABLED", ""
        ).lower() in {"1", "true", "yes"}
    advisor["endpoint"] = os.getenv(
        "MARKET_ADVISOR_ENDPOINT",
        advisor.get("endpoint", "http://127.0.0.1:8765/v1/judge"),
    )
    if os.getenv("MARKET_ADVISOR_TIMEOUT_SECONDS") is not None:
        advisor["timeout_seconds"] = int(
            os.getenv("MARKET_ADVISOR_TIMEOUT_SECONDS", "3")
        )

    raw["config_dir"] = base_dir
    raw["log_dir"] = Path(os.getenv("TRADING_LOG_DIR", project_root / "logs"))
    raw["okx"] = {
        "name": exchange_raw.get("name", "okx"),
        "base_url": os.getenv(
            "OKX_BASE_URL", exchange_raw.get("base_url", "https://www.okx.com")
        ),
        "user_agent": os.getenv(
            "OKX_USER_AGENT",
            exchange_raw.get(
                "user_agent", "open-trade/0.1 (OKX REST client)"
            ),
        ),
        "simulated_trading": os.getenv(
            "OKX_SIMULATED_TRADING", str(exchange_raw.get("simulated_trading", False))
        ).lower()
        in {"1", "true", "yes"},
        "use_private_read": os.getenv(
            "OKX_USE_PRIVATE_READ", str(exchange_raw.get("use_private_read", False))
        ).lower()
        in {"1", "true", "yes"},
        "default_inst_type": exchange_raw.get("instruments", {}).get(
            "default_inst_type", "SWAP"
        ),
        "trust_env_proxy": os.getenv(
            "OKX_TRUST_ENV_PROXY", str(exchange_raw.get("trust_env_proxy", False))
        ).lower()
        in {"1", "true", "yes"},
        "max_read_retries": int(
            os.getenv("OKX_MAX_READ_RETRIES", exchange_raw.get("max_read_retries", 2))
        ),
        "retry_backoff_seconds": float(
            os.getenv(
                "OKX_RETRY_BACKOFF_SECONDS",
                exchange_raw.get("retry_backoff_seconds", 1.0),
            )
        ),
        "api_key": os.getenv("OKX_API_KEY") or None,
        "api_secret": os.getenv("OKX_API_SECRET") or None,
        "api_passphrase": os.getenv("OKX_API_PASSPHRASE") or None,
    }
    return AppConfig.model_validate(raw)


def _resolve_config_dir(config_dir: str | Path | None = None) -> Path:
    if config_dir is not None:
        return Path(config_dir)
    env_dir = os.getenv("TRADING_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    local_dir = Path("config")
    if (local_dir / "app.yaml").exists():
        return local_dir
    return local_dir
