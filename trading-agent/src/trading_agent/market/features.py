"""Domain adapter; indicator formulas are owned by the shared trading-quant package."""

from dataclasses import asdict
from datetime import datetime

from trading_quant import CalculationError, QuantConfig
from trading_quant import compute_features as calculate
from trading_quant.spec import CORE_FEATURES

from trading_agent.config import FeatureConfig
from trading_agent.domain.errors import DomainError
from trading_agent.market.models import Bar, Feature, FormingCandle, Observation, ProvisionalFeature


def compute_features(
    bars: tuple[Bar, ...],
    config: FeatureConfig,
    observations: tuple[Observation, ...] = (),
    decision_at: datetime | None = None,
    *,
    benchmark_bars: tuple[Bar, ...] = (),
) -> tuple[Feature, ...]:
    try:
        return tuple(
            Feature(**asdict(value))
            for value in calculate(
                bars, config, observations, decision_at, benchmark_bars=benchmark_bars
            )
        )
    except CalculationError as exc:
        raise DomainError(exc.code, exc.message) from exc


def compute_provisional_features(
    bars: tuple[Bar, ...],
    forming: FormingCandle,
    config: FeatureConfig,
    decision_at: datetime,
) -> tuple[ProvisionalFeature, ...]:
    """Reuse the shared formulas on 80 closed bars plus one observed partial bar."""
    forming = FormingCandle.model_validate(forming)
    if len(bars) != 80:
        raise DomainError("BAR_COUNT", "临时指标需要80根已收盘K线及1根当前快照")
    if (
        any(
            b.inst_id != forming.inst_id
            or b.timeframe != forming.timeframe
            or b.close_boundary > decision_at
            or b.available_at > decision_at
            for b in bars
        )
        or any(a.close_boundary != b.open_at for a, b in zip(bars, bars[1:], strict=False))
        or bars[-1].close_boundary != forming.open_at
        or forming.available_at > decision_at
        or decision_at >= forming.close_boundary
    ):
        raise DomainError("FORMING_CANDLE_SCOPE", "临时指标窗口或快照时点不一致")
    return provisional_values(bars, forming, config, decision_at, event_at=forming.available_at)


def provisional_values(
    bars, forming, config, calculated_at, *, event_at, source_prefix="provisional"
):
    """Shared formula/tag adapter; live and reconstruction callers validate their own clocks."""
    selected = set(config.required_features + config.optional_features)
    # External observations are not derived from an unfinished OHLCV bar. Optional
    # temporary values also must not become a new required-feature trading gate.
    core_config = QuantConfig(
        required_features=(),
        optional_features=tuple(name for name in CORE_FEATURES if name in selected),
    )
    try:
        values = calculate((*bars, forming), core_config, decision_at=calculated_at)
    except CalculationError as exc:
        raise DomainError(exc.code, exc.message) from exc
    result = []
    for value in values:
        fields = asdict(value)
        caveat = None
        if value.name in {"rvol", "volume_zscore"}:
            caveat = "PARTIAL_VOLUME_VS_FULL_HISTORY_NOT_TIME_NORMALIZED"
            if value.quality == "OK":
                fields.update(
                    quality="PARTIAL_COVERAGE",
                    missing_reason="当前累计成交量与历史完整周期比较，未按相同进度校正，不能据此确认缩量",
                )
        elif value.name == "breakout_strength":
            caveat = "INTRABAR_NOT_CLOSE_CONFIRMED"
        elif value.name == "vwap_deviation":
            caveat = "ROLLING_20_CANDLES_INCLUDING_PARTIAL_CURRENT_VOLUME"
        fields.update(
            source=f"{source_prefix}:{value.source}",
            event_at=event_at,
            available_at=calculated_at,
        )
        result.append(ProvisionalFeature(**fields, caveat=caveat))
    return tuple(result)
