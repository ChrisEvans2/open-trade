from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from trading_agent.config import FeatureConfig, StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import TIMEFRAMES, boundary, require_anchor, utc
from trading_agent.market.features import compute_features
from trading_agent.market.models import Bar, Feature, Observation


def validate_bars(
    bars: tuple[Bar, ...], anchor: datetime, timeframe: str, decision_at: datetime, count: int = 80
) -> None:
    anchor, decision_at = require_anchor(anchor), utc(decision_at)
    if len(bars) != count:
        raise DomainError("BAR_COUNT", f"{timeframe}必须包含{count}根已收盘K线")
    if len({b.inst_id for b in bars}) != 1 or len({b.source for b in bars}) != 1:
        raise DomainError("MIXED_MARKET", "K线混入不同标的或来源")
    if any(b.timeframe != timeframe or b.available_at > decision_at for b in bars):
        raise DomainError("FUTURE_DATA", "K线周期不一致或冻结时点之后才可用")
    if any(a.close_boundary != b.open_at for a, b in zip(bars, bars[1:], strict=False)):
        raise DomainError("BAR_GAP", "K线存在缺口、重复或乱序")
    if bars[-1].close_boundary != boundary(anchor, timeframe):
        raise DomainError("BAR_BOUNDARY", "最后一根K线的UTC收盘边界错误")


class TimeframeInput(StrictModel):
    timeframe: str
    bars: tuple[Bar, ...]
    model_bar_ids: tuple[UUID, ...]
    features: tuple[Feature, ...]
    observation_ids: tuple[UUID, ...] = ()
    feature_spec_hash: str

    @model_validator(mode="after")
    def model_view_is_tail(self):
        if self.model_bar_ids != tuple(b.id for b in self.bars[-30:]):
            raise ValueError("模型视图必须为计算窗口末尾30根")
        return self

    @property
    def input_content_hash(self) -> str:
        # Re-fetch clocks and the current hour/job/case list cannot invalidate a trend cache.
        return content_hash(
            {
                "timeframe": self.timeframe,
                "feature_spec_hash": self.feature_spec_hash,
                "bars": [
                    {
                        "id": b.id,
                        "revision_no": b.revision_no,
                        "content_hash": b.market_content_hash,
                    }
                    for b in self.bars
                ],
                "features": [f.model_dump(exclude={"available_at"}) for f in self.features],
                "observation_ids": self.observation_ids,
            }
        )


class Snapshot(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    anchor_at: datetime
    decision_at: datetime
    feature_spec_version: str
    groups: tuple[TimeframeInput, ...]

    @model_validator(mode="after")
    def validate_groups(self):
        require_anchor(self.anchor_at)
        utc(self.decision_at)
        if tuple(group.timeframe for group in self.groups) != TIMEFRAMES:
            raise ValueError("快照必须包含有序的1h/4h/6h/12h输入")
        for group in self.groups:
            validate_bars(group.bars, self.anchor_at, group.timeframe, self.decision_at)
        if len({group.bars[-1].inst_id for group in self.groups}) != 1:
            raise ValueError("四周期标的不一致")
        return self

    @property
    def payload_hash(self) -> str:
        return content_hash(self.model_dump(exclude={"id"}))

    def group(self, timeframe: str) -> TimeframeInput:
        return next(group for group in self.groups if group.timeframe == timeframe)


def build_snapshot(
    anchor: datetime,
    decision_at: datetime,
    data: dict[str, tuple[Bar, ...]],
    feature_config: FeatureConfig,
    observations: tuple[Observation, ...] = (),
) -> Snapshot:
    groups = []
    for timeframe in TIMEFRAMES:
        bars = data[timeframe]
        validate_bars(bars, anchor, timeframe, decision_at)
        end = decision_at if timeframe == "1h" else bars[-1].close_boundary
        selected = tuple(
            o
            for o in observations
            if o.inst_id == bars[-1].inst_id
            and o.available_at <= decision_at
            and o.event_at
            <= (decision_at if o.kind == "funding" and "published_rate" in o.payload else end)
            and o.payload.get("timeframe") in {None, timeframe}
        )
        groups.append(
            TimeframeInput(
                timeframe=timeframe,
                bars=bars,
                model_bar_ids=tuple(b.id for b in bars[-30:]),
                features=compute_features(bars, feature_config, selected, decision_at),
                observation_ids=tuple(o.id for o in selected),
                feature_spec_hash=content_hash(feature_config),
            )
        )
    return Snapshot(
        anchor_at=anchor,
        decision_at=decision_at,
        feature_spec_version=feature_config.spec_version,
        groups=tuple(groups),
    )
