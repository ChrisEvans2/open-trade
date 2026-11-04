from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from trading_quant.spec import CORE_FEATURES, EXTRA_FEATURES

from trading_agent.domain.serialization import content_hash


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )


class TradeMinuteConfig(StrictModel):
    raw_retention_hours: int = Field(default=24, ge=1, le=168)
    bootstrap_days: int = Field(default=11, ge=1, le=30)
    repair_lookback_days: int = Field(default=30, ge=10, le=80)
    poll_seconds: int = Field(default=15, ge=5, le=60)
    settle_delay_seconds: int = Field(default=10, ge=5, le=120)
    cycle_budget_seconds: int = Field(default=40, ge=5, le=120)
    max_pages_per_minute: int = Field(default=300, ge=1, le=1000)
    max_requests_per_cycle: int = Field(default=300, ge=5, le=1000)
    max_minutes_per_cycle: int = Field(default=12, ge=2, le=60)
    retry_seconds: int = Field(default=60, ge=10, le=3600)
    audit_interval_minutes: int = Field(default=60, ge=5, le=1440)

    @model_validator(mode="after")
    def bounded_history(self):
        if self.bootstrap_days > self.repair_lookback_days:
            raise ValueError("逐笔初始回补范围不能超过修复回溯范围")
        return self


class MarketConfig(StrictModel):
    source: Literal["okx_contract_public"] = "okx_contract_public"
    base_url: str = "https://www.okx.com"
    credentials_file: Path | None = None
    public_ws_url: str = "wss://ws.okx.com/ws/v5/public"
    symbol: Literal["BTCUSDT", "ETHUSDT"] = "BTCUSDT"
    inst_id: str | None = None
    contract_type: Literal["perpetual", "delivery"] | None = None
    settlement_asset: str | None = None
    taker_timestamp: Literal["open", "close"] | None = None
    taker_source: Literal["trade_minutes", "rubik"] = "trade_minutes"
    trade_minutes: TradeMinuteConfig = Field(default_factory=TradeMinuteConfig)
    oi_history_timestamp: Literal["instant", "open", "close"] | None = None
    long_short_timestamp: Literal["instant", "open", "close"] | None = None
    calculation_bars_per_interval: Literal[80] = 80
    agent_bars_per_interval: Literal[30] = 30
    review_bars_per_interval: Literal[80] = 80
    clock_skew_seconds: int = Field(default=5, ge=1, le=5)
    connect_timeout_seconds: float = 3
    read_timeout_seconds: float = 10
    retries: Literal[3] = 3

    @model_validator(mode="after")
    def public_channel_only(self):
        if self.credentials_file is not None and not self.credentials_file.is_absolute():
            raise ValueError("OKX 凭据文件必须使用绝对路径")
        endpoint = urlsplit(self.public_ws_url)
        if (
            endpoint.scheme != "wss"
            or not endpoint.hostname
            or endpoint.path != "/ws/v5/public"
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("清算采样只允许无凭据的公开wss频道")
        return self

    def require_instrument(self):
        if not all((self.inst_id, self.contract_type, self.settlement_asset)):
            raise ValueError("合约 inst_id、contract_type、settlement_asset 未确定")


class FeatureConfig(StrictModel):
    spec_version: str = "ohlcv_wilder_v1"
    required_features: tuple[str, ...] = ("return_1", "return_20", "atr_pct", "rsi")
    optional_features: tuple[str, ...] = tuple(
        k
        for k in CORE_FEATURES + EXTRA_FEATURES
        if k not in {"return_1", "return_20", "atr_pct", "rsi"}
    )
    wilder_window: Literal[14] = 14
    regression_window: Literal[20] = 20
    volume_window: Literal[20] = 20
    volatility_window: Literal[10] = 10
    volatility_baseline: Literal[60] = 60
    funding_baseline_events: Literal[30] = 30
    ddof: Literal[0] = 0
    rsi_flat: Literal[50] = 50
    market_basket: tuple[str, ...] = ()
    market_basket_min_coverage: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)

    @model_validator(mode="after")
    def validate_sets(self):
        required, optional = set(self.required_features), set(self.optional_features)
        allowed = set(CORE_FEATURES + EXTRA_FEATURES)
        if required & optional or (required | optional) - allowed:
            raise ValueError("特征集合重复或含未知特征")
        if len(required) != len(self.required_features) or len(optional) != len(
            self.optional_features
        ):
            raise ValueError("特征清单不可重复")
        if len(set(self.market_basket)) != len(self.market_basket):
            raise ValueError("市场篮子不可重复")
        if len(self.market_basket) > 50 or any(
            not 1 <= len(member) <= 128 for member in self.market_basket
        ):
            raise ValueError("市场篮子最多50个明确instId，单项不超过128字符")
        return self


class ForecastConfig(StrictModel):
    anchor_interval_seconds: Literal[3600] = 3600
    snapshot_start_seconds: Literal[60] = 60
    snapshot_deadline_seconds: Literal[120] = 120
    publish_deadline_seconds: Literal[240] = 240
    entry_offset_seconds: Literal[300] = 300
    horizon_seconds: Literal[3600] = 3600
    deadline_inclusive: Literal[True] = True
    label_threshold: Decimal = Field(default=Decimal("0.005"), gt=0, lt=1)


class PaperConfig(StrictModel):
    enabled: bool = False
    contract_spec_version: str | None = None
    initial_collateral: Decimal | None = Field(default=None, gt=0)
    settlement_asset: str | None = None
    margin_mode: Literal["isolated", "cross"] | None = None
    leverage: Decimal | None = Field(default=None, ge=1)
    position_equity_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    fee_rate: Decimal | None = Field(default=None, ge=0, lt=1)
    slippage_rate: Decimal | None = Field(default=None, ge=0, lt=1)
    maintenance_margin_rate: Decimal | None = Field(default=None, ge=0, lt=1)
    funding_model: Literal["observed_events"] | None = None
    direction_rule: Literal["highest_direction_above_threshold"] | None = None
    initial_entry_threshold: Decimal | None = Field(default=None, ge=Decimal("0.5"), le=1)
    stop_loss_rate: Decimal | None = Field(default=None, gt=0, lt=1)
    take_profit_rate: Decimal | None = Field(default=None, gt=0)
    risk_interval_seconds: Literal[60] = 60

    @model_validator(mode="after")
    def require_explicit_rules(self):
        if self.enabled:
            needed = (
                "contract_spec_version",
                "initial_collateral",
                "settlement_asset",
                "margin_mode",
                "leverage",
                "position_equity_fraction",
                "fee_rate",
                "slippage_rate",
                "maintenance_margin_rate",
                "funding_model",
                "direction_rule",
                "initial_entry_threshold",
            )
            missing = [key for key in needed if getattr(self, key) is None]
            if missing:
                raise ValueError("模拟合约规则未确定: " + ", ".join(missing))
        return self


class MemoryConfig(StrictModel):
    case_lookback_days: Literal[90] = 90
    max_cases: Literal[5] = 5
    max_observations: Literal[3] = 3
    observation_ttl_days: Literal[30] = 30
    context_max_chars: Literal[6000] = 6000


class ExperimentConfig(StrictModel):
    enabled: bool = False
    thresholds: tuple[Decimal, ...] = tuple(map(Decimal, ("0.55", "0.60", "0.65", "0.70", "0.75")))
    train_size: Literal[80] = 80
    forward_size: Literal[40] = 40
    min_train_trades: Literal[10] = 10
    min_forward_trades: Literal[8] = 8


class PredictionConfig(StrictModel):
    """Initial operational choices, included in engine identity and frozen inputs."""

    mode: Literal["structural"] = "structural"
    symbols: tuple[Literal["BTCUSDT", "ETHUSDT"], ...] = ("BTCUSDT", "ETHUSDT")
    timeframes: tuple[Literal["1h", "4h", "6h", "12h"], ...] = ("1h", "4h", "6h", "12h")
    structure_version: Literal["confirmed_pivots_v1"] = "confirmed_pivots_v1"
    tolerance_atr: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    initial_horizon_bars: Literal[2, 3, 4, 6, 8, 12] = 4
    horizon_candidates: tuple[Literal[2, 3, 4, 6, 8, 12], ...] = (2, 3, 4, 6, 8, 12)
    replay_min_samples: int = Field(default=40, ge=40)
    shadow_min_samples: int = Field(default=20, ge=20)
    monitor_batch_samples: int = Field(default=20, ge=20)
    minimum_score_gain: Decimal = Field(default=Decimal("0.02"), ge=0)
    maximum_failure_increase: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    waiting_cost_per_bar: Decimal = Field(default=Decimal("0.01"), ge=0, le=1)
    context_max_age_seconds: int = Field(default=210, ge=60, le=300)
    knowledge_index_path: Path = Path(".local/prediction-knowledge.sqlite3")
    execution_enabled: bool = False
    legacy_config_dir: Path = Path("../config")
    account_key: str = "prediction-main"

    @model_validator(mode="after")
    def unique_scopes(self):
        for values in (self.symbols, self.timeframes, self.horizon_candidates):
            if not values or len(values) != len(set(values)):
                raise ValueError("预测范围和候选期限必须非空且不重复")
        if "1h" not in self.timeframes:
            raise ValueError("父Agent的1h交易尺度必须存在")
        return self


class RuntimeConfig(StrictModel):
    api_host: Literal["127.0.0.1"] = "127.0.0.1"
    api_port: int = Field(default=8765, ge=1024, le=65535)
    api_workers: Literal[1] = 1
    scheduler_tick_seconds: Literal[60] = 60
    executor_poll_seconds: Literal[2] = 2
    notifier_poll_seconds: Literal[5] = 5
    recovery_tick_seconds: Literal[15] = 15
    lease_seconds: Literal[60] = 60
    heartbeat_seconds: Literal[15] = 15
    deterministic_concurrency: Literal[4] = 4
    price_refresh_lookback_days: int = Field(default=90, ge=1, le=3650)
    price_refresh_batch_size: int = Field(default=24, ge=1, le=168)
    parent_agent_concurrency: Literal[2] = 2
    agent_timeout_seconds: Literal[150] = 150
    initial_agent_budget_seconds: Literal[120] = 120
    analysis_round_seconds: Literal[1200] = 1200
    analysis_window_seconds: Literal[30] = 30
    shutdown_grace_seconds: Literal[20] = 20
    kill_grace_seconds: float = Field(default=3, gt=0, le=10)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=10000)
    db_lock_timeout_ms: int = Field(default=2000, ge=100, le=5000)
    loop_stale_multiplier: int = Field(default=3, ge=2, le=5)
    data_dir: Path = Path(".local")
    log_max_bytes: int = Field(default=10_000_000, ge=100_000)
    log_backup_count: int = Field(default=10, ge=1)
    log_queue_size: int = Field(default=1000, ge=100)


class HermesConfig(StrictModel):
    executable: Path | None = None
    python_executable: Path | None = None
    home: Path | None = None
    repository: Path | None = None
    reference_commit: str = "71cb7d60f8469768e597e39d9ddb682a4e20dbfc"
    controller_profile: str | None = None
    trading_profile: str = "trading"
    trading_telegram_enabled: bool = False
    provider: str | None = None
    model: str | None = None
    model_base_url: str | None = None
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None
    deployment_lock: Path = Path("deployment.lock.json")

    @model_validator(mode="after")
    def credential_free_endpoint(self):
        if self.model_base_url is not None:
            endpoint = urlsplit(self.model_base_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("模型端点须为不含凭据、query 或 fragment 的 HTTP(S) 地址")
        return self


class SandboxConfig(StrictModel):
    base_url: str | None = None
    template: str | None = None
    sdk_version: str | None = None
    proxy_node_ip: str | None = None
    proxy_port: int = Field(default=80, ge=1, le=65535)
    sandbox_domain: str = "cube.app"
    management_timeout_seconds: float = Field(default=8, gt=0, le=15)
    execution_user: str = "research"
    cpu_count: int = Field(default=2, ge=1)
    memory_mib: int = Field(default=2048, ge=256)
    disk_mib: int = Field(default=4096, ge=256)
    max_processes: int = Field(default=128, ge=16)
    max_output_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    idle_timeout_seconds: int = Field(default=180, ge=120, le=600)
    renew_interval_seconds: float = Field(default=15, ge=1, le=60)
    timeout_grace_seconds: int = Field(default=15, ge=5, le=60)
    denied_networks: tuple[str, ...] = ()
    isolation_verified: bool = False
    isolation_report: Path | None = None

    @model_validator(mode="after")
    def forbid_root(self):
        if self.execution_user in {"root", "0"}:
            raise ValueError("研究命令必须使用普通用户")
        if (
            self.renew_interval_seconds
            + 2 * self.management_timeout_seconds
            + self.timeout_grace_seconds
            >= self.idle_timeout_seconds
        ):
            raise ValueError("沙箱超时必须覆盖续期间隔、管理请求和清理余量")
        return self


class NotificationConfig(StrictModel):
    target: str | None = None
    display_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    daily_summary_max_chars: Literal[600] = 600
    send_timeout_seconds: Literal[10] = 10
    attempts: Literal[5] = 5
    retry_seconds: tuple[int, ...] = (60, 300, 900, 900)
    claim_lease_seconds: Literal[120] = 120


class AppConfig(StrictModel):
    schema_version: Literal[2] = 2
    market: MarketConfig = Field(default_factory=MarketConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    experiment: ExperimentConfig = Field(default_factory=ExperimentConfig)
    prediction: PredictionConfig = Field(default_factory=PredictionConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)

    @model_validator(mode="after")
    def compatible_modes(self):
        if self.experiment.enabled and not self.paper.enabled:
            raise ValueError("收益实验依赖已确定的模拟合约规则")
        if self.paper.enabled and self.paper.settlement_asset != self.market.settlement_asset:
            raise ValueError("模拟账户与行情合约结算资产不一致")
        return self

    @property
    def engine_config_hash(self) -> str:
        # Execution settings do not change the target; installation adds prompt hashes.
        hermes = self.hermes.model_dump(include={"reference_commit", "provider", "model"})
        # Preserve existing identities when these optional model settings are absent.
        hermes.update(
            self.hermes.model_dump(
                include={"model_base_url", "reasoning_effort"}, exclude_none=True
            )
        )
        return content_hash(
            {
                "market": self.market.model_dump(
                    include={
                        "source",
                        "symbol",
                        "inst_id",
                        "contract_type",
                        "settlement_asset",
                        "taker_timestamp",
                        "taker_source",
                        "trade_minutes",
                        "oi_history_timestamp",
                        "long_short_timestamp",
                        "calculation_bars_per_interval",
                        "agent_bars_per_interval",
                    }
                ),
                "features": self.features,
                "forecast": self.forecast,
                "hermes": hermes,
                "analysis_mode": "one_hour_with_trend_context_v1",
                "trend_schema_version": 1,
                "observation_contract_version": "okx_public_observations_v5_trade_minutes",
            }
        )


def load_config(path: Path) -> AppConfig:
    with path.open(encoding="utf-8") as stream:
        return AppConfig.model_validate(yaml.safe_load(stream) or {})
