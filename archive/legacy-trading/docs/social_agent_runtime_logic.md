> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 社媒 Agent 运行逻辑

本文记录当前社媒消息分析链路的实际运行方式。对应主要代码与配置：

- `/srv/open-trade-deployment/src/trading_system/scheduler.py`
- `/srv/open-trade-deployment/src/trading_system/social/monitor.py`
- `/srv/open-trade-deployment/src/trading_system/social/filter.py`
- `/srv/open-trade-deployment/src/trading_system/social/sources.py`
- `/srv/open-trade-deployment/src/trading_system/ai/analyzer.py`
- `/srv/open-trade-deployment/config/social_sources.yaml`

## 职责边界

社媒 agent 的职责是把公告、新闻、社媒消息整理成结构化的风险与事件 alpha 结果。它不直接下单，不连接交易所执行接口，不修改账户状态，不提高仓位或杠杆。

社媒链路的输出只会影响两个位置：

1. 在策略信号合成阶段，作为 `SocialContributionAudit` 记录，并可能把技术信号降级为 `HOLD`，或在技术策略为空时生成小仓位事件信号。
2. 在风控阶段，作为 `AIAnalysisResult` 参与社媒风险过滤和仓位乘数计算。

最终是否开仓、开多大、用多少杠杆，仍由 `/srv/open-trade-deployment/src/trading_system/risk/engine.py` 的 `RiskEngine.evaluate` 裁决。

## 数据来源

社媒来源由 `/srv/open-trade-deployment/config/social_sources.yaml` 配置，启动时通过 `build_sources` 构建。

当前启用的来源包括：

- Bybit Announcements，官方交易所公告，`trust: 0.9`。
- OKX Latest Announcements，OKX 公告 API，`trust: 0.9`，带缓存 TTL。
- BlockBeats Pro Newsflash，BlockBeats Pro 快讯，`trust: 0.86`，需要 `BLOCKBEATS_API_KEY` 或 `BLOCKBEAT_API_KEY`。

当前未启用但代码支持的来源包括：

- RSS feed。
- GitHub releases。
- X API v2。
- Telegram bot updates。
- fixture 测试源。

`SocialMonitor.fetch_recent` 会并发拉取所有启用来源。单个来源失败不会中断整轮交易循环，失败会写 stderr 和 logger，其他来源继续返回。

## 预过滤逻辑

`SocialFilter.filter_items` 对原始消息做轻量过滤，目标是减少无关消息和重复消息进入 AI 分析。

主要规则：

- 只保留 `lookback_hours` 时间窗口内的消息。
- 按 `(source, text[:240])` 去重。
- 如果消息自带 `asset`，则必须匹配当前资产或当前交易对。
- 如果消息不带 `asset`，需要命中当前资产、交易对，或广义市场关键词。
- 过滤明显垃圾内容，例如 `airdrop claim`、`guaranteed profit`、`100x`、`send funds`、`private key`。
- 按 `source_trust`、`engagement`、`time` 倒序排序。
- 默认最多保留 50 条。

广义市场关键词包括 BTC、ETH、crypto、链上、交易所、stablecoin、ETF、SEC、CFTC、监管、hack、exploit、美联储、利率、CPI、PCE、非农、通胀等。

## AI 分析输入

`SocialAIAnalyzer.analyze` 接收：

- 当前 `symbol` 和去掉 `USDT` 后的 `asset`。
- 当前 `MarketSnapshot`。
- 过滤后的 `SocialItem` 列表。
- 权限边界说明。

传给模型的权限边界固定包含：

- analyzer 没有执行权限。
- analyzer 不得建议提高仓位。
- 输出必须是 JSON。

如果本轮没有社媒消息，系统不会调用模型，而是返回低置信度中性结果。

## AI 输出结构

模型输出会被归一化并校验为 `AIAnalysisResult`，核心字段包括：

- `sentiment`：消息情绪。
- `event_risk`：`low`、`medium`、`high`、`critical`。
- `should_block_trade`：是否建议阻断新开仓。
- `position_multiplier`：社媒风险给出的仓位乘数，范围 0 到 1。
- `market_relevance`：`direct_asset`、`crypto_sector`、`macro_market`、`political_geopolitical`、`unrelated`。
- `relevance_score`：相关性评分。
- `alpha_signal`：`long`、`short`、`hold`。
- `alpha_confidence`：事件 alpha 置信度。
- `alpha_horizon`：`scalp`、`intraday`、`swing`、`none`。
- `event_type`：listing、delisting、hack、regulation、macro、etf_flow、onchain_flow、funding、partnership、upgrade、market_structure、other、none。
- `source_weight`：来源可信度。
- `freshness_score`：新鲜度。
- `evidence` 和 `key_events`：审计证据。
- `analysis_status` 和 `analysis_error`：分析状态和错误信息。

模型输出资产必须匹配当前资产或交易对，否则按资产不匹配降级处理。

## 失败降级

如果未启用 LiteLLM client，社媒分析会走本地启发式规则。

如果模型调用失败、JSON schema 错误或资产不匹配，系统调用 `_fallback`：

- 默认 `event_risk=medium`。
- `alpha_signal=hold`，禁用消息面 alpha。
- 如果 `ai_failure_policy == "block_new_entries"`，则 `should_block_trade=True`，`position_multiplier=0`。
- 否则按技术信号降级，`position_multiplier=0.5`。

失败降级不代表真实消息面高风险，只代表 AI 分析链路当前不可用。

## 启发式分析

本地启发式规则用于无模型或模型不可用时的保守兜底。

主要识别：

- hack、exploit、SEC investigation、lawsuit、delist、rug、暂停提现、被盗等重大负面词，标记高风险并阻断。
- ETF approval、partnership、listing、mainnet、upgrade、上线等正面词，可能生成 long alpha。
- 战争升级、热冲突、伊朗、能源通胀、CPI、加息、ETF 净流出、减仓、卖出等宏观风险词，标记 medium 风险和潜在 short alpha。
- 政治或地缘消息如果缺少明确市场传导路径，按低相关度处理。
- 定投、长期配置、长期持有等慢变量叙事会被压制，不生成短线 alpha。

## 与策略信号合成

社媒分析后，`TradingScheduler._combine_event_alpha_signal` 会把 `AIAnalysisResult` 合成进 `StrategySignal`。

当技术策略已经给出 `LONG` 或 `SHORT`：

- 如果社媒 alpha 与技术方向一致，只记录一致性，不提高仓位。
- 如果社媒 alpha 与技术方向冲突，且满足高置信条件，技术入场会被降级为 `HOLD`。
- 高置信冲突条件包括：`alpha_confidence >= 0.70`、`source_weight >= 0.70`、`freshness_score >= 0.20`，且消息相关性不是 `unrelated`。

当技术策略为 `HOLD`：

- 如果存在高风险阻断信号，一般交给风控处理。
- 如果是确认过的防御性 short 场景，可生成 `defensive_social_short`。
- 普通事件 alpha 需要满足：`alpha_confidence >= 0.65`、`source_weight >= 0.70`、`freshness_score >= 0.20`、相关性不是 `unrelated`。
- 事件 alpha 还要通过市场状态约束。例如非下跌结构里不能轻易创建 short，非上涨结构里不能轻易创建 long。
- 创建的事件信号默认是小仓位，`suggested_position_pct <= 0.03`。

防御性 social short 需要更严格：

- `analysis_status` 为 `ok` 或 `heuristic`。
- `alpha_signal == SHORT`。
- `alpha_confidence >= 0.75`。
- `source_weight >= 0.80`。
- `freshness_score >= 0.50`。
- 市场结构为 `WATERFALL`，或 `BREAKOUT_DOWN` 且有新低确认。

## 风控交互

社媒结果进入 `RiskEngine.evaluate` 后仍会再次检查：

- `should_block_trade=True` 或 `event_risk` 为 high/critical 时，默认阻断新开仓。
- 防御性 social short 是例外，但会被标记为 `defensive_short_reduced_position`，仓位乘数减半。
- `event_risk=medium` 时，仓位乘数最多 0.5。
- `event_risk=low` 时，仓位乘数不超过模型给出的 `position_multiplier`，且最大为 1。

社媒 agent 永远不能绕过暂停、emergency、亏损限制、重复持仓限制、最大持仓限制等风控规则。

## 审计与落盘

每轮 cycle 的社媒输入和输出会进入 `CycleArtifacts`：

- `social_raw`
- `social_filtered`
- `ai_analysis`
- `strategy_signal.social_audit`

`AuditLogger.save_cycle` 会保存完整 cycle 审计。开仓或平仓事件里还会记录：

- `signal_source`
- `technical_signal`
- `social_alpha_signal`
- `social_changed_decision`
- `social_contribution_reason`
- `entry_reasons`

这些字段用于后续复盘判断一笔交易是技术信号、社媒信号，还是混合信号驱动。

## 安全原则

- 社媒 agent 只能降风险或提供小仓位候选，不能提高全局风险。
- 社媒消息不能直接触发交易所订单。
- 模型输出必须经过 schema 校验和本地代码合成。
- 模型失败时按保守策略降级，不把失败当作真实行情判断。
- 长期配置类叙事不作为短线入场依据。
