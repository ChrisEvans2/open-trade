"""Minute-based historical evidence, never relabeled as a decision-time observation."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import INTERVAL_SECONDS, TIMEFRAMES, boundary, utc
from trading_agent.market.features import provisional_values
from trading_agent.market.models import Bar
from trading_agent.market.snapshots import validate_bars


@dataclass(frozen=True)
class ReconstructedCandle:
    # Separate from FormingCandle: the historical rows can be collected much later.
    inst_id: str
    timeframe: str
    open_at: datetime
    close_boundary: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_contracts: Decimal
    volume_base: Decimal
    turnover_quote: Decimal
    target_at: datetime
    data_cutoff: datetime
    collected_at: datetime
    status: str = "RECONSTRUCTED"
    source: str = "okx_contract_public_1m"

    @property
    def available_at(self):
        return self.collected_at


def reconstruct_candle(minutes, *, inst_id, timeframe, target_at, collected_by):
    """Aggregate [period start, floor(target, 1m)); exclude the unfinished target minute."""
    target_at, collected_by = utc(target_at), utc(collected_by)
    if timeframe not in TIMEFRAMES or inst_id not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}:
        raise DomainError("RECONSTRUCTION_SCOPE", "重建标的或周期无效")
    if target_at > collected_by:
        raise DomainError("FUTURE_DATA", "不能重建未来的盘中状态")
    start, cutoff = boundary(target_at, timeframe), boundary(target_at, "1m")
    if start == cutoff:
        raise DomainError("NO_COMPLETED_MINUTE", "当前周期尚无完整分钟，不能重建盘中K线")
    bars = tuple(sorted((Bar.model_validate(b) for b in minutes), key=lambda b: b.open_at))
    if any(
        b.inst_id != inst_id
        or b.source != "okx_contract_public"
        or b.timeframe != "1m"
        or b.open_at < start
        or b.close_boundary > cutoff
        or b.available_at > collected_by
        for b in bars
    ):
        raise DomainError("RECONSTRUCTION_SCOPE", "分钟数据混入其他范围或截止时间之后的数据")
    expected = int((cutoff - start).total_seconds() // 60)
    if len(bars) != expected or any(
        b.open_at != start + timedelta(minutes=i) for i, b in enumerate(bars)
    ):
        raise DomainError("MINUTE_COVERAGE_GAP", "分钟数据不连续或覆盖不足，不补值")
    return ReconstructedCandle(
        inst_id=inst_id,
        timeframe=timeframe,
        open_at=start,
        close_boundary=start + timedelta(seconds=INTERVAL_SECONDS[timeframe]),
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume_contracts=sum(b.volume_contracts for b in bars),
        volume_base=sum(b.volume_base for b in bars),
        turnover_quote=sum(b.turnover_quote for b in bars),
        target_at=target_at,
        data_cutoff=cutoff,
        collected_at=max(b.available_at for b in bars),
    )


def reconstruct_input(original, closed_bars, minutes, config, *, reconstructed_at):
    """Supplemental review evidence, computed from the original closed-bar revisions."""
    body = original["body"]
    target = utc(original["decision_at"])
    candle = reconstruct_candle(
        minutes,
        inst_id=body["inst_id"],
        timeframe=original["timeframe"],
        target_at=target,
        collected_by=reconstructed_at,
    )
    if [str(b.id) for b in closed_bars] != body["bar_ids"]:
        raise DomainError("ORIGINAL_BARS_MISSING", "缺少原始计算窗口的K线版本")
    validate_bars(closed_bars, boundary(target), candle.timeframe, target)
    if any(b.inst_id != candle.inst_id for b in closed_bars):
        raise DomainError("RECONSTRUCTION_SCOPE", "原始窗口混入其他标的")
    values = provisional_values(
        closed_bars,
        candle,
        config,
        reconstructed_at,
        event_at=candle.data_cutoff,
        source_prefix="reconstructed:provisional",
    )
    # Keep compact minute evidence in the new review fact; no original input is changed.
    columns = (
        "open_at",
        "open",
        "high",
        "low",
        "close",
        "volume_contracts",
        "volume_base",
        "turnover_quote",
        "available_at",
    )
    minute_source = json_value(
        {
            "columns": columns,
            "rows": [
                [getattr(b, name) for name in columns]
                for b in sorted(minutes, key=lambda b: b.open_at)
            ],
        }
    )
    return json_value(
        {
            "quality": "OK" if target == candle.data_cutoff else "PARTIAL_COVERAGE",
            "missing_reason": None if target == candle.data_cutoff else "SUBMINUTE_NOT_AVAILABLE",
            "evidence_kind": "RECONSTRUCTED_FROM_1M",
            "originally_observed": False,
            "availability_at_target_verified": False,
            "reconstructed_at": reconstructed_at,
            "precision_seconds": 60,
            "uncovered_seconds": (target - candle.data_cutoff).total_seconds(),
            "forming_candle": asdict(candle),
            "provisional_features": values,
            "feature_basis": {
                "closed_bars": 80,
                "forming_bars": 1,
                "scope": "OHLCV_ONLY",
                "volume_time_normalized": False,
                "source": "ORIGINAL_CLOSED_BAR_REVISIONS",
            },
            "minute_source_hash": content_hash(minute_source),
            "minute_source": minute_source,
        }
    )
