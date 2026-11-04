"""Explicit evidence limits and decision-time funding context for frozen inputs."""

from decimal import Decimal

BTC_RELATIVE = ("btc_correlation", "btc_beta", "relative_strength_vs_btc")


def indicator_policy(basket):
    return {
        "version": 2,
        "weak_features": list(BTC_RELATIVE),
        "btc_relative": {
            "evidence_strength": "WEAK",
            "allowed_use": "辅助描述联动和相对强弱",
            "prohibited_use": "不得单独确认方向、结构、突破或触发交易；三项不能重复计为独立证据",
            "window_returns": 20,
            "relative_strength_definition": "(asset_end/asset_start)/(btc_end/btc_start)-1",
        },
        "funding_rate": {
            "basis": "DECISION_TIME_PUBLISHED_ESTIMATE",
            "required": False,
            "meaning": "当前预计结算费率，结算前可变化；不是长周期收盘时的费率",
        },
        "funding_zscore": {
            "basis": "SETTLED_EVENTS_1H_AT_DECISION_LONG_AT_CANDLE_CLOSE",
            "baseline_events": 30,
        },
        "taker_volume": {
            "meaning": (
                "当前合约最近20根已完成区间的主动买卖量，CVD为窗口买量减卖量；"
                "具体来源以指标source为准"
            ),
            "minute_collection": (
                "默认由确定性代码采集、按分钟聚合/校验/补抓，模型只读已冻结结果；"
                "覆盖不足不猜买卖方向"
            ),
            "direction_limit": "总量对齐不能证明买卖方向正确；独立分类复算仍依赖官方逐笔side字段",
            "volume_disagreement": (
                "与同区间K线量不一致时降为PARTIAL_COVERAGE；不猜测缺少成交的买卖方向"
            ),
        },
        "market_breadth": {
            "basket": list(basket),
            "meaning": "固定篮子中收盘价高于自身SMA20的有效成员占比，非全市场广度",
            "minimum_coverage": "由features.market_basket_min_coverage决定",
        },
        "liquidation_imbalance": {
            "evidence_strength": "WEAK",
            "coverage": "OKX_PUBLIC_SAMPLE_ONLY",
            "fallback": "可结合价格、OI变化和主动成交观察去杠杆迹象，但不能称为强平金额或确认强平",
        },
    }


def funding_context(observations, decision_at):
    eligible = sorted(
        (
            o
            for o in observations
            if o.kind == "funding"
            and o.quality == "OK"
            and o.available_at <= decision_at
            and o.event_at <= decision_at
        ),
        key=lambda o: (o.event_at, o.revision_no),
    )
    by_time = {o.event_at: o for o in eligible if o.payload.get("event_type") == "settled"}
    events = [by_time[t] for t in sorted(by_time)][-9:]
    history = []
    for previous, current in zip(events, events[1:], strict=False):
        hours = Decimal(str((current.event_at - previous.event_at).total_seconds())) / 3600
        # Spacing between returned events does NOT prove the venue settlement interval.
        rate = Decimal(str(current.payload["realized_rate"]))
        history.append(
            {
                "event_at": current.event_at,
                "available_at": current.available_at,
                "observation_id": current.id,
                "previous_observation_id": previous.id,
                "realized_rate": rate,
                "observed_event_spacing_hours": hours,
            }
        )
    rates = [r["realized_rate"] for r in history]
    regular = len({r["observed_event_spacing_hours"] for r in history}) == 1
    return {
        "as_of": decision_at,
        "quality": "INSUFFICIENT_HISTORY"
        if len(rates) < 8
        else "OK"
        if regular
        else "PARTIAL_COVERAGE",
        "missing_reason": "需要9个独立结算事件观察最近8次费率及间隔"
        if len(rates) < 8
        else None
        if regular
        else "结算事件间隔不一致，可能调频或缺事件，不输出可比趋势摘要",
        "settled_history": history,
        "mean_per_settlement_rate": sum(rates) / len(rates) if rates and regular else None,
        "latest_change_per_settlement": rates[-1] - rates[-2]
        if len(rates) >= 2 and regular
        else None,
        "spacing_regular": regular,
        "settlement_interval_verified": False,
        "positive_events": sum(r > 0 for r in rates),
        "negative_events": sum(r < 0 for r in rates),
        "caveat": (
            "原始每次结算费率，未按小时归一化；间隔相等也不能证明接口没有缺事件。"
            "不是未来收益，不能单独确认多空方向"
        ),
    }
