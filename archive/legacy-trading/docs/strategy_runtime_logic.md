> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 策略板块运行逻辑

本文记录当前策略板块的实际运行方式。对应主要代码与配置：

- `/srv/open-trade-deployment/src/trading_system/scheduler.py`
- `/srv/open-trade-deployment/src/trading_system/strategy/freqtrade_style.py`
- `/srv/open-trade-deployment/src/trading_system/strategy/market_regime.py`
- `/srv/open-trade-deployment/src/trading_system/strategy/trend_strategy.py`
- `/srv/open-trade-deployment/src/trading_system/ai/market_advisor.py`
- `/srv/open-trade-deployment/config/strategy.yaml`

## 总体位置

策略板块负责生成 `StrategySignal`。它不直接下单，也不决定最终仓位和杠杆。

主循环顺序是：

1. 同步账户权益、实盘已实现 PnL、实盘持仓状态。
2. 拉取最新已收盘 K 线。
3. 更新持仓 mark price、资金费和浮盈浮亏。
4. 拉取并分析社媒消息。
5. 生成技术策略信号。
6. 合并社媒事件 alpha。
7. 如启用 market advisor，则由 LLM 网关提出候选，再由本地代码确认。
8. 交给 `RiskEngine.evaluate` 做最终裁决。

策略板块输出的是候选信号，最终执行必须经过风控。

## 当前默认策略

当前 `/srv/open-trade-deployment/config/strategy.yaml` 中 `freqtrade_style.enabled: true`，所以默认使用 `FreqtradeStyleStrategy`。

如果关闭 `freqtrade_style.enabled`，系统会退回到较简单的 `TrendStrategy`。当前运行逻辑以 Freqtrade 风格策略为主。

## 输入数据

`TradingScheduler._generate_strategy_signal` 会优先调用：

```text
market_data.fetch_recent_candles(symbol, timeframe)
strategy.generate_signal_from_candles(candles, market)
```

策略需要足够 K 线数量。当前 `startup_candle_count: 80`，如果 K 线数量不足，直接返回 `HOLD`，`invalid_reason=["insufficient_candles"]`。

`market` 使用最新已收盘 K 线快照，包含 open、high、low、close、volume、timestamp、timeframe 等。

## 技术指标

`FreqtradeStyleStrategy.populate_indicators` 会计算：

- `ema_fast`：默认 12。
- `ema_slow`：默认 26。
- `rsi`：默认 14。
- `atr` 和 `atr_pct`：默认 14。
- `volume_mean` 和 `volume_ratio`：默认 20 根均量。
- `adx`：默认 14。
- `ema_separation_pct`：快慢 EMA 距离。
- `ema_fast_slope_pct`：快 EMA 斜率，默认回看 3 根。
- `price_extension_atr`：价格相对快 EMA 的 ATR 延伸倍数。

当前配置里的主要阈值：

- 多头 RSI 下限：`rsi_long_min: 55`。
- 空头 RSI 上限：`rsi_short_max: 42`。
- ATR 最小比例：`atr_min_pct: 0.001`。
- 最小成交量比例：`min_volume_ratio: 1.0`。
- 最小 ADX：`min_adx: 25.0`。
- 最小 EMA 斜率：`min_ema_slope_pct: 0.0015`。
- 最小 EMA 分离度：`min_ema_separation_pct: 0.0015`。
- 最大价格延伸：`max_price_extension_atr: 1.5`。
- 默认建议保证金比例：`default_position_pct: 0.08`。

## 趋势入场条件

多头入场需要同时满足：

- `ema_fast > ema_slow`。
- `close > ema_fast`。
- `rsi >= rsi_long_min`。
- `atr_pct >= atr_min_pct`。
- `volume_ratio >= min_volume_ratio`。
- `adx >= min_adx`。
- `ema_separation_pct >= min_ema_separation_pct`。
- `ema_fast_slope_pct >= min_ema_slope_pct`。
- `price_extension_atr <= max_price_extension_atr`。

空头入场需要同时满足：

- `ema_fast < ema_slow`。
- `close < ema_fast`。
- `rsi <= rsi_short_max`。
- `atr_pct >= atr_min_pct`。
- `volume_ratio >= min_volume_ratio`。
- `adx >= min_adx`。
- `ema_separation_pct >= min_ema_separation_pct`。
- `ema_fast_slope_pct <= -min_ema_slope_pct`。
- `price_extension_atr <= max_price_extension_atr`。

如果多空条件都不满足，返回 `HOLD`，`invalid_reason=["freqtrade_style_no_entry"]`。

## 市场状态分类

策略内置 `MarketRegimeClassifier`，用于判断当前结构是否适合趋势或区间交易。

分类器基于最近 K 线计算：

- 趋势效率 `trend_efficiency`。
- 波动率比率 `volatility_ratio`。
- EMA 方向与斜率。
- ADX。
- 区间高低点、有效区间、区间位置。
- 当前区间是否扩张。
- 最近是否反复突破区间高低点。
- change point score。
- shock return z 和 range z。

输出 `MarketRegimeSnapshot`，主要字段：

- `regime`：`trend_up`、`trend_down`、`range`、`squeeze`、`choppy_high_vol`、`transition`、`shock`、`unknown`。
- `detailed_regime`：如 `TREND_UP`、`RANGE_WIDE`、`BREAKOUT_UP`、`WATERFALL`、`SHOCK` 等。
- `pattern`：如 `smooth_trend`、`relief_rally`、`pullback`、`v_reversal`、`false_breakout`、`range_edge`。
- `trend_direction`。
- `confidence`。
- `risk_flags` 和 `block_reasons`。
- `range_low`、`range_high`、`range_mid`、`range_quality_score`。

市场状态分类器还会记录上一个状态和当前状态持续 bars，用于避免刚切换 regime 时过早入场。

## 趋势信号的 regime 过滤

`regime_filter_enabled: true` 时，趋势信号必须通过市场状态过滤。

会阻断趋势入场的常见情况：

- 当前为 `shock`、`choppy_high_vol`、`transition`。
- `detailed_regime` 为 `WATERFALL`、`EXPANDING_RANGE`、`RANGE_HIGH_VOL`、`SHOCK`、`CHOPPY_HIGH_VOL`、`TRANSITION`。
- 刚从不稳定状态切入趋势，且稳定 bars 少于 `trend_min_bars_before_entry`。
- 当前为 `range`，阻断趋势入场。
- 当前为 `squeeze`，等待突破，不提前趋势入场。
- transition 中出现 v reversal、inverted v、false breakout 或无明确形态。
- 趋势效率低于 `min_trend_efficiency: 0.35`。
- 做空时，如果下跌趋势效率低于 `short_min_trend_efficiency: 0.45`，或 regime 置信度低于 `short_min_regime_confidence: 0.65`，会被 short rebound guard 阻断。
- 做空遇到 `relief_rally` 或 `v_reversal`。
- 做多遇到非上升趋势中的 `pullback` 或 `inverted_v`。
- 多空方向与 `trend_up`、`trend_down` 或 `BREAKOUT_UP`、`BREAKOUT_DOWN` 冲突。

通过过滤后，趋势信号会返回：

- `signal=LONG` 或 `SHORT`。
- `confidence`，由趋势强度、RSI 动量和成交量计算。
- `entry_reason`，记录 EMA、ADX、RSI、ATR、成交量、价格延伸等通过原因。
- `suggested_position_pct`，不超过 `MAX_SINGLE_TRADE_MARGIN_PCT`。
- `stop_loss_pct`，默认为 `max(ATR% * 2, atr_min_pct)`。
- `take_profit_pct`，趋势入场默认约为止损的 2 倍。
- `market_regime`，用于审计和后续风控。

## 区间均值回归逻辑

如果 `range_trading_enabled: true`，策略会先尝试区间信号，再尝试趋势信号。

区间信号要求：

- `regime.regime == "range"`。
- `regime.pattern == "range_edge"`。
- 有有效 `range_position`。

做多区间条件：

- 当前接近区间下沿，`range_position <= range_edge_threshold`，当前配置为 0.22。
- RSI 足够低，`rsi <= range_rsi_long_max`，当前为 38。
- 通过 `_range_invalid_reasons` 检查。

做空区间条件：

- 当前接近区间上沿，`range_position >= 1 - range_edge_threshold`。
- RSI 足够高，`rsi >= range_rsi_short_min`，当前为 62。
- 通过 `_range_invalid_reasons` 检查。

区间信号特点：

- `entry_reason` 包含 `range_regime`、区间边缘、RSI 超买/超卖、`mean_reversion_entry`。
- 默认建议仓位为 `range_position_pct: 0.04`。
- 风险收益比默认 1.2。
- 止损在区间边界外加 ATR buffer。
- 止盈目标偏向 `range_mid`。

会阻断区间入场的情况包括：

- high volatility。
- `RANGE_HIGH_VOL`、`BREAKOUT_UP`、`BREAKOUT_DOWN`、`WATERFALL`、`EXPANDING_RANGE`。
- 区间扩张。
- regime 置信度低于 `range_min_confidence: 0.65`。
- 区间质量低于 `range_quality_threshold: 0.65`。
- 当前 regime 尚未稳定，bars 少于 `min_bars_before_entry: 3`。
- `RANGE_WIDE` 下置信度不足、质量不足或冷却 bars 不足。
- 做空遇到 fresh upper break，做多遇到 fresh lower break。

## 社媒 alpha 合成

技术策略信号生成后，调度器会调用 `_combine_event_alpha_signal` 合成社媒结果。

合成原则：

- 社媒 alpha 与技术信号一致时，只记录，不放大仓位。
- 社媒 alpha 与技术信号高置信冲突时，可把技术入场降级为 `HOLD`。
- 技术策略为 `HOLD` 时，社媒 alpha 可在严格条件下生成小仓位事件信号。
- 事件信号仍带有止损、止盈、原因和社媒审计字段。
- 最终还必须进入风控。

## Market Advisor 逻辑

如果 `market_advisor.enabled` 为 true，策略信号还会进入 market advisor 流程。

该流程不是执行器，而是一个行情判断辅助网关：

1. 调度器按配置收集多个周期 K 线，默认 12h、4h、1h、15m。
2. 构造脱敏 payload，只包含行情、策略信号、本地 regime、BTC/ETH 方向摘要和权限边界。
3. 发送给本地 HTTP 网关。
4. 网关返回多周期 advisor 结果和 meta decision。
5. 本地 `CodeSafetyGate` 做二次确认。
6. 只有代码确认通过，才会把 `HOLD` 转为早期结构候选信号。

market advisor 的权限边界：

- 不能交易。
- 不能访问账户。
- 不能访问本地文件。
- 不能改变风控。
- 最终决定由本地代码和风控完成。

早期结构候选还要满足本地硬规则，例如突破结构位、成交量达标、BTC/ETH 方向确认、当前 regime 不在阻断列表、止损距离合理、仓位和杠杆封顶等。

## 输出与审计

策略板块最终输出 `StrategySignal`，关键字段：

- `signal`：`long`、`short`、`hold`、`close`。
- `confidence`。
- `entry_reason`。
- `invalid_reason`。
- `suggested_position_pct`。
- `stop_loss_pct`。
- `take_profit_pct`。
- `atr_pct`。
- `risk_reward_ratio`。
- `market_regime`。
- `social_audit`。
- `advisor_judgement`、`advisor_confidence`、`advisor_confirmed_by_code`。
- `risk_budget_pct` 和 `max_leverage_override`，用于后续风控动态 sizing。

调度器会把策略信号写入 cycle 审计目录，并在交易事件中记录 `signal_source`、`strategy_type`、`entry_reasons`、regime 和 advisor 字段。

## 安全原则

- 策略只生成候选信号，不直接下单。
- 策略建议仓位受 `MAX_SINGLE_TRADE_MARGIN_PCT` 和风控上限限制。
- 止损、止盈、ATR、regime 等字段会传给执行和审计。
- 社媒和 advisor 只能作为辅助信号，不能绕过 `RiskEngine.evaluate`。
- market advisor 失败时回退原策略，不影响原策略安全边界。
