> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 风控板块运行逻辑

本文记录当前风控板块的实际运行方式。对应主要代码与配置：

- `/srv/open-trade-deployment/src/trading_system/risk/engine.py`
- `/srv/open-trade-deployment/src/trading_system/execution/paper_risk.py`
- `/srv/open-trade-deployment/src/trading_system/scheduler.py`
- `/srv/open-trade-deployment/config/risk.yaml`
- `/srv/open-trade-deployment/config/runtime_state.yaml`
- `/srv/open-trade-deployment/state/live_account.json`
- `/srv/open-trade-deployment/state/paper_account.json`

## 总体职责

风控板块是新开仓的最终硬门。策略、社媒和 market advisor 都只能给出候选信号，最终是否允许交易由 `RiskEngine.evaluate` 返回的 `RiskDecision` 决定。

风控负责：

- 判断当前运行状态是否允许新开仓。
- 检查账户健康、数据健康、亏损、回撤、连续亏损。
- 检查暂停、emergency、单币暂停。
- 检查最大持仓数量、重复持仓、同向集中暴露。
- 检查社媒风险。
- 根据止损距离和风险预算动态计算保证金比例与杠杆。
- 返回最终动作、仓位、杠杆和风险检查结果。

风控不负责生成交易方向。方向来自 `StrategySignal`。

## 风控配置加载

风控参数由 `/srv/open-trade-deployment/config/risk.yaml` 加载。

加载顺序：

1. `defaults`。
2. 当前 `execution_mode` 对应的覆盖值，例如 `execution_modes.live`。
3. 当前 `mode` 对应的覆盖值，例如 `modes.cautious`、`modes.paused`。

当前主要默认值：

- `max_position_pct: 0.10`
- `min_position_margin: 2.0`
- `min_leverage: 3.0`
- `max_leverage: 20.0`
- `max_daily_loss_pct: 0.03`
- `max_single_trade_loss_pct: 0.010`
- `max_drawdown_pct: 0.08`
- `max_consecutive_losses: 4`
- live 模式覆盖为 `max_consecutive_losses: 3`
- `max_open_positions: 8`
- `max_same_direction_positions: 2`
- `max_same_direction_margin_pct: 0.10`
- `max_notional_pct: 0.60`
- `stop_loss_pct: 0.015`
- `take_profit_pct: 0.03`
- trailing stop 相关默认值：`trailing_stop_pct: 0.010`、`trailing_activation_min_pct: 0.008`、`trailing_activation_max_pct: 0.020`
- `allow_short: true`
- `allow_new_entries: true`

`modes.paused` 和 `modes.emergency` 都会覆盖为 `allow_new_entries: false`。

## 运行状态输入

风控读取 `RuntimeState`：

- `mode`：`paper`、`normal`、`cautious`、`paused`、`emergency`。
- `execution_mode`：`paper` 或 `live`。
- `global_paused`。
- `emergency`。
- `paused_symbols`。
- `hard_stop_reason` 和 `hard_stop_at`。
- `order_failure_count`。

如果 `runtime_state.emergency == true`，风控模式强制视为 `EMERGENCY`。

## 账户状态输入

风控读取 `AccountState`：

- `equity`
- `daily_pnl_pct`
- `drawdown_pct`
- `consecutive_losses`
- `open_positions`
- 每个持仓的杠杆、入场价、mark price、止损止盈、浮盈浮亏、资金费等。
- `exchange_healthy`
- `data_healthy`

实盘模式下，调度器会在每轮 cycle 前尝试同步权益、已实现 PnL 和交易所持仓。

## 平仓优先原则

如果 `StrategySignal.signal == CLOSE` 且该 symbol 有持仓，风控直接返回 reduce-only close：

- `allow_trade=True`
- `final_action=CLOSE`
- 仓位等于当前持仓绝对值。
- 杠杆沿用当前持仓杠杆。
- reason 为只允许 reduce-only/close 平仓。

这条路径用于允许平仓优先于新开仓限制。

## 新开仓阻断顺序

除平仓外，风控按以下顺序检查新开仓。

### 运行状态

如果 `global_paused` 为 true，或 mode 是 `PAUSED`、`EMERGENCY`：

- 返回 `HOLD`。
- reason：系统处于暂停或 emergency 模式，只允许人工处理或平仓。

如果当前 symbol 在 `paused_symbols`：

- 返回 `HOLD`。
- reason：该 symbol 已暂停交易。

### 健康检查

如果 `account.exchange_healthy` 或 `account.data_healthy` 为 false：

- 返回 `HOLD`。
- reason：交易所或数据健康检查失败。

### 亏损与回撤

如果 `daily_pnl_pct <= -max_daily_loss_pct`：

- 返回 `HOLD`。
- reason：触发单日最大亏损限制。

如果 `drawdown_pct >= max_drawdown_pct`：

- 返回 `HOLD`。
- reason：触发最大回撤限制。

如果 `consecutive_losses >= max_consecutive_losses`：

- 返回 `HOLD`。
- reason：连续亏损次数达到限制。

### 无策略信号

如果 `StrategySignal.signal == HOLD`：

- 返回 `HOLD`。
- reason：代码策略无交易信号。

### 开仓权限

如果是新开仓信号，但 `allow_new_entries` 为 false：

- 返回 `HOLD`。
- reason：当前风控配置禁止新开仓。

如果信号为 `SHORT`，但 `allow_short` 为 false：

- 返回 `HOLD`。
- reason：当前风控配置禁止做空。

### 持仓数量和重复开仓

如果当前持仓数量已达到 `max_open_positions`，且当前 symbol 不在持仓中：

- 返回 `HOLD`。
- reason：同时持仓数量达到上限。

如果当前 symbol 已有持仓：

- 返回 `HOLD`。
- reason：已有同 symbol 持仓，禁止重复开仓。

### 同向集中暴露

对新开仓方向计算已有同方向持仓：

- 如果同方向持仓数量达到 `max_same_direction_positions`，阻断。
- 如果同方向保证金占比达到 `max_same_direction_margin_pct`，阻断。

当前配置中同方向最多 2 个仓位，同方向保证金占比最多 10%。

## 社媒风险处理

风控读取 `AIAnalysisResult`。

如果 `ai.should_block_trade` 为 true，或 `event_risk` 是 high/critical：

- 默认阻断新开仓。
- 例外是 `defensive_social_short`，并且策略信号是 `SHORT`。

防御性 social short 通过时：

- 不直接放大风险。
- 设置 `social_risk_filter=defensive_short_reduced_position`。
- 仓位乘数为 0.5。

普通社媒风险乘数：

- `event_risk=low`：`position_multiplier` 最高按 1 处理。
- `event_risk=medium`：最多 0.5。
- 其他风险等级：0。

社媒乘数只会降低仓位，不会提高策略建议仓位或全局风险上限。

## 动态仓位与杠杆

通过所有阻断检查后，风控调用 `_risk_sizing` 计算实际保证金比例和杠杆。

核心输入：

- 策略信号止损 `signal.stop_loss_pct`，缺失时使用配置 `stop_loss_pct`。
- 策略信号风险预算 `signal.risk_budget_pct`，缺失时使用 `max_single_trade_loss_pct`。
- 社媒风险乘数。
- 当前账户权益。
- 策略建议仓位 `signal.suggested_position_pct`。
- 全局 `max_position_pct`。
- 当前 mode 的仓位上限。
- `MAX_SINGLE_TRADE_MARGIN_PCT`。
- `max_notional_pct`。
- `min_position_margin`。
- `min_leverage`、`max_leverage`。
- `signal.max_leverage_override`。

计算逻辑：

1. 止损距离至少为极小正数，避免除零。
2. 单笔风险预算取 `max_single_trade_loss_pct` 与 `signal.risk_budget_pct` 的较小值，再乘社媒乘数。
3. 最小保证金比例为 `min_position_margin / equity`。
4. 最大保证金比例取策略建议、全局上限、模式上限、单笔硬上限的最小值，再乘社媒乘数。
5. 目标名义仓位为 `risk_budget_pct / stop_loss_pct`，再受 `max_notional_pct` 限制。
6. 如果目标名义仓位过小，低于最小保证金和最小杠杆要求，则跳过交易。
7. 如果目标名义仓位可用最小杠杆完成，则用最小杠杆。
8. 否则计算所需杠杆，但不得超过最大杠杆。
9. 保证金比例、杠杆、名义仓位四舍五入到 6 位。

如果计算出的仓位小于最低保证金要求，风控返回 `HOLD`。

## 不同 mode 的仓位上限

`_mode_position_cap` 当前规则：

- `CAUTIOUS`：最多 3% 保证金。
- `PAPER`：最多 10% 保证金。
- `NORMAL`：最多 10% 保证金。
- 其他模式：0。

这个上限还会与配置中的 `max_position_pct`、策略建议仓位和单笔硬上限共同取最小值。

## 风控输出

风控返回 `RiskDecision`：

- `allow_trade`：是否允许交易。
- `final_action`：`HOLD`、`OPEN_LONG`、`OPEN_SHORT`、`CLOSE`。
- `final_position_pct`：最终保证金比例。
- `final_leverage`：最终杠杆。
- `risk_mode`：当前风控模式。
- `risk_checks`：每项检查的 pass/fail/blocked 状态。
- `reason`：中文原因，包含策略方向、社媒风险、止损、风险预算、保证金、名义仓位和杠杆等。

执行器只应使用 `RiskDecision`，不应绕过风控直接使用策略建议。

## 持仓退出与 mark 同步

新开仓风控由 `RiskEngine` 负责，持仓退出还会通过 `PaperPositionRiskManager` 和执行器处理。

调度器在每轮 cycle 和 mark sync 中会：

- 更新 mark price。
- 更新浮盈浮亏。
- 检查止损、止盈、移动止盈等退出条件。
- 如果需要平仓，paper 模式直接生成 close result，live 模式调用执行器。

`mark_update` 只是持仓状态刷新，不等同于闭环交易。

## Hard Stop 与订单失败

调度器会在风控决策后调用 hard stop 相关逻辑，并记录订单失败次数。

当实盘 reconcile 失败或订单失败达到阈值时，系统可以进入 emergency 或 hard stop 状态，阻断后续新开仓。具体状态持久化在 runtime state 中。

## 审计与落盘

风控结果会进入每轮 `CycleArtifacts`：

- `strategy_signal`
- `ai_analysis`
- `risk_decision`
- `execution_result`

交易事件中会记录：

- `margin_pct`
- `margin_amount`
- `leverage`
- `notional_amount`
- `stop_loss_pct`
- `take_profit_pct`
- `trailing_stop_pct`
- `trailing_activation_pct`
- `risk_flags`
- `reason`

实盘事件写入 `/srv/open-trade-deployment/logs/live_trade_events.jsonl`，paper 事件写入 `/srv/open-trade-deployment/logs/paper_trade_events.jsonl`。

## 安全原则

- 风控是最终硬门，任何策略、社媒、advisor 输出都不能绕过。
- AI 只能降低风险，不能提高仓位或杠杆。
- 暂停、emergency、亏损、回撤、重复持仓和集中暴露优先于任何入场信号。
- 平仓路径应保持 reduce-only 优先。
- 对 100 USDT 级别账户，最低保证金和风险预算共同限制交易频率，避免小账户过度交易。
