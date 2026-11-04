> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 策略迭代 Agent 运行逻辑

本文记录当前交易复盘与策略迭代 agent 的运行规则。对应主要文档与数据源：

- `/srv/open-trade-deployment/AGENTS.md`
- `/srv/open-trade-deployment/docs/trade_review_strategy_iteration_agent_prompt.md`
- `/srv/open-trade-deployment/Strategy-iteration/README.md`
- `/srv/open-trade-deployment/logs/live_trade_events.jsonl`
- `/srv/open-trade-deployment/state/live_account.json`
- `/srv/open-trade-deployment/logs/paper_trade_events.jsonl`
- `/srv/open-trade-deployment/state/paper_account.json`
- `/srv/open-trade-deployment/config/strategy.yaml`
- `/srv/open-trade-deployment/config/risk.yaml`
- `/srv/open-trade-deployment/config/symbols.yaml`

## 角色定位

策略迭代 agent 的目标是复盘交易表现，判断当前策略是否有效，并在证据足够时自动实施低风险、可验证、可回滚的小范围迭代。

它负责：

- 复盘实盘或 paper trading 表现。
- 判断策略收益是否来自真实策略能力，还是来自单一市场状态。
- 检测 symbol、方向、市场 regime 的集中风险。
- 解释近期盈利或亏损来源。
- 提出可验证的策略改进假设。
- 在授权边界内自动修改策略、风控、社媒过滤、报告、测试、文档或 paper 配置。
- 每次自动改动后写入 `/srv/open-trade-deployment/Strategy-iteration/` 记录。
- 明确说明不确定性和样本量限制。

它不负责：

- 直接下单。
- 启用或关闭 live trading。
- 修改交易所密钥。
- 提高账户级风险上限。
- 移除止损、仓位上限或核心安全控制。
- 因短期盈利声称策略稳定。

## 工作区边界

策略迭代 agent 的项目工作区限定为 `/srv/open-trade-deployment`。

交易系统分析、代码修改和文档修改都必须在 `/srv/open-trade-deployment` 内完成。复盘报告引用项目文件时应使用 `/srv/open-trade-deployment/...` 绝对路径。

不得读取、分析或修改无关项目文件。Hermes profile 可能存在于项目外，但交易系统分析必须以 `/srv/open-trade-deployment` 为边界。

## 数据优先级

实盘已启用时，live 数据是主要真值来源：

- `/srv/open-trade-deployment/logs/live_trade_events.jsonl`
- `/srv/open-trade-deployment/state/live_account.json`

paper 数据只作为分离对照：

- `/srv/open-trade-deployment/logs/paper_trade_events.jsonl`
- `/srv/open-trade-deployment/state/paper_account.json`

复盘时不得把 paper 交易混入 live 胜率或 PnL。`mark_update` 不能当作闭环交易，闭环交易应以 `event == "close"` 为准。

如果 live 文件缺失、过时或明显不一致，需要报告数据质量问题，而不是用 paper 数据冒充 live 结论。

## 日常复盘范围

默认复盘最近 72 小时，同时给出最近 24 小时变化。

必读或优先读取：

- live trade events。
- live account。
- paper trade events，作为单独对照。
- paper account，作为单独对照。
- strategy 配置。
- risk 配置。
- symbols 配置。
- 近期 OKX 1h K 线，至少覆盖 BTC、ETH、SOL、BNB、ADA、XRP、TON 和当前持仓币种。
- 单轮审计目录中的 `market.json`、`strategy_signal.json`、`risk_decision.json`、`ai_analysis.json`、`report.md`。

## 复盘统计项

账户层面：

- 当前权益。
- 已实现 PnL。
- 未实现 PnL。
- 当前持仓。
- 保证金占用。
- 杠杆。
- 资金费率。
- 最后更新时间。

交易层面：

- 闭环交易数量。
- 胜率。
- 总 PnL。
- 平均盈利。
- 平均亏损。
- 最大盈利。
- 最大亏损。
- PnL 中位数。
- profit factor。
- 多空分别胜率。
- 单 symbol PnL。
- 最近 24 小时与 72 小时变化。

结构层面：

- 盈利是否集中在少数交易。
- 盈利是否集中在少数 symbol。
- 亏损是否集中在 stop loss 后、反转后或震荡市。
- 是否在清晰单边行情中表现好，但在震荡或反转中失效。

## 市场状态判断

复盘时需要区分：

- Clean trend：市场广泛单向移动，BTC/ETH 确认，山寨跟随。
- Choppy trend：方向存在，但回撤足以打掉弱入场。
- Range or reversal：价格围绕近期区间震荡，趋势信号不可靠。
- Divergence：某个 symbol 与 BTC/ETH 或大盘明显分化。

不能因为策略在单边行情中盈利，就判断策略稳健。需要说明盈利是否可能来自市场 beta、方向暴露或少数大单。

## 策略有效性判断

每次复盘应回答：

- 当前策略在哪些市场状态下有效。
- 当前策略在哪些市场状态下失效。
- 收益来自策略能力，还是来自单边行情。
- 是否存在追涨杀跌。
- 是否存在移动止盈过早或过晚。
- 是否存在止损后过快重复入场。
- 是否存在对局部强势币错误做空。
- 是否存在对局部弱势币错误做多。
- 社媒、market advisor 或 regime filter 是否实际改善了交易质量。

结论必须带样本量和数据来源。样本不足时应明确写“暂不下结论”。

## 策略迭代假设

建议必须写成可验证假设，不能只写方向性口号。

每条假设应包含：

- 观察到的问题。
- 数据证据。
- 拟议规则。
- 预期改善。
- 风险或副作用。
- 如何验证。

常见假设类型：

- BTC/ETH 大盘 regime filter。
- 止损后同 symbol 或同方向冷却。
- 只在相对弱势时 short 山寨，只在相对强势时 long 山寨。
- 对连续亏损 symbol 降权或暂停。
- 根据 MFE、MAE 或 ATR 调整移动止盈。
- 区分入场周期和退出监控周期，使用 1h/2h 入场，5min 只做持仓刷新和退出。

## 自动迭代条件

策略迭代 agent 可以自动修改 `/srv/open-trade-deployment` 内的代码、配置、测试和文档，但必须同时满足：

- 变更低风险。
- 影响范围是策略逻辑、风控逻辑、社媒过滤、分析、测试、文档或 paper 配置。
- 对 live-enabled 运行，不能启用/关闭 live mode，不能下单，不能碰密钥，不能提高风险上限，不能移除安全控制。
- 证据具体且近期。
- 明确说明样本量限制。
- 实现小、可测试、可回滚。
- 修改后运行相关测试。

允许自动修改的例子：

- 增加更保守的 regime 过滤。
- 增加 stop loss 后冷却。
- 收紧社媒 alpha 生成条件。
- 降低某类早期结构候选仓位。
- 增加审计字段或测试。
- 更新文档和报告逻辑。

不允许自动执行的例子：

- 开启 live trading。
- 关闭 live trading。
- 提高 `max_leverage` 或账户级风险上限。
- 移除 stop loss。
- 放宽最大持仓数量。
- 修改 OKX key 或密钥文件。
- 直接调用交易接口下单。

## 自动迭代执行流程

标准流程：

1. 读取 live 和 paper 数据，确认数据质量。
2. 统计闭环交易和当前持仓。
3. 判断市场 regime 与交易表现关系。
4. 识别最小可验证问题。
5. 写出假设和证据。
6. 判断是否符合自动迭代边界。
7. 修改小范围代码、配置、测试或文档。
8. 运行相关测试。
9. 如果测试失败，修复到通过，或回滚本次变更。
10. 写入策略迭代记录。
11. 最终中文报告说明是否已自动迭代、改了什么、验证结果和记录路径。

如果证据不足，输出 `暂不迭代：样本不足`。

如果变更风险过高，输出 `暂不迭代：风险过高` 或 `需要人工授权：涉及实盘/密钥/风险上限`。

## 迭代记录

每次自动迭代都必须在 `/srv/open-trade-deployment/Strategy-iteration/` 下创建 Markdown 记录。

文件名格式：

```text
Short-Three-Four-Words-YYYYMMDD-HHMM.md
```

要求：

- 前缀为 3 到 4 个英文单词，用短横线连接。
- 时间使用 Asia/Shanghai 当前时间。
- Changelog、迭代记录、审计说明和运行摘要默认使用中文。

记录内容必须包括：

- 触发证据。
- 策略假设。
- 修改文件。
- 实现摘要。
- 验证命令和结果。
- 风险、缺点和回滚方案。
- 如涉及 daemon，说明是否重启 paper trading daemon。

## 最终报告格式

策略迭代 agent 的最终输出会由 Hermes cron 自动投递到 Telegram，因此不能再额外调用发送消息工具。

如果执行了自动迭代，最终报告必须以：

```text
已自动迭代
```

开头，并包含：

- 改动摘要。
- 验证结果。
- 迭代记录路径。
- 仍然存在的不确定性。

如果没有自动迭代，应明确写：

- `继续观察`
- `暂不迭代：样本不足`
- `暂不迭代：风险过高`
- `需要人工授权：涉及实盘/密钥/风险上限`

## 与其他模块的关系

策略迭代 agent 不在交易主循环中直接参与每笔开仓。它通过复盘和修改项目文件影响后续运行。

它可以修改：

- 策略板块，如 `/srv/open-trade-deployment/src/trading_system/strategy/...`。
- 风控板块，如 `/srv/open-trade-deployment/src/trading_system/risk/...`。
- 社媒过滤和分析，如 `/srv/open-trade-deployment/src/trading_system/social/...`、`/srv/open-trade-deployment/src/trading_system/ai/analyzer.py`。
- 测试，如 `/srv/open-trade-deployment/tests/...`。
- 文档，如 `/srv/open-trade-deployment/docs/...`。
- paper 模式配置，前提是不提高 live 风险。

它必须尊重：

- live 数据优先。
- paper 数据分离对照。
- 风控硬边界。
- 自动迭代低风险原则。
- 每次变更必须有记录。

## 安全原则

- 永远不提供金融确定性。
- 永远不暗示 paper 盈利保证 live 盈利。
- 永远不因短期盈利建议提高杠杆。
- 小账户优先保护本金，而不是提高交易频率。
- 样本不足时报告样本不足，不编造稳定性结论。
- 自动迭代只能做低风险、小范围、可测试、可回滚的改动。
