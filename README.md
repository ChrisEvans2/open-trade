# Open Trade

BTC、ETH 的多周期结构与趋势预测驱动交易。唯一运行入口是 `trading-agent/` 中的
`ta --config <配置文件> serve`；先完成部署校验，再启动一个 runtime。

用户交互链路保留：**你 / TG ⇄ Hermes Gateway ⇄ 主控 Hermes ⇄ 交易 runtime ⇄ 独立预测 Hermes**。
主控负责对话、查询预测/持仓/运行状态、申请解释和复盘；预测 Hermes 内的父/子 Agent 负责
分析与交易意图，两者使用隔离 Profile。通知复用现有 outbox 与 HermesSender，经主控 Profile
发送，不另建网关。一个业务 runtime 不等于只保留一个 Hermes 角色。

| 目录 | 职责 |
| --- | --- |
| `trading-agent/` | 唯一 runtime：行情证据、各周期预测、父 Agent 综合、知识库、复盘学习、持久化执行与 API |
| `packages/trading-core/` | 共享交易核心：确定性风控、原有风险参数、仓位记账、退出判断及执行客户端；没有 CLI 或调度器 |
| `packages/trading-quant/` | 共享量化库：指标与结构几何计算；没有模型、账户或订单入口 |
| `config/` | 通用暂停示例；不包含操作者现用参数、账户或币池 |
| `archive/` | 旧交易框架、旧预测流程和旧说明的历史参考；不安装、不部署，不作为运行入口 |

父 Agent 输出交易意图；程序验证预测有效性、持仓绑定和现有风控后处理执行。
模型不能调用订单接口或提高风险上限。预测到期不自动平仓。
PostgreSQL 保存正式预测、评分、交易与审计事实；SQLite/FTS5 是可重建的知识检索投影。
历史表、迁移和历史查询保持原义，清理目录不会删除历史数据。

## 可能存在的问题：逐笔成交与分钟聚合

OKX 已通过用户提供的客服回复确认主动成交统计接口存在数据问题，尚未完成修复验收。
本地已实现使用公开历史逐笔生成分钟买卖聚合，由确定性代码采集、检测及补抓，Agent 只读取
结果和质量标记。分钟聚合长期保存，原始逐笔默认压缩留存24小时；实现和实际部署验收分别记录。
实现、默认配置与验证边界见[分钟聚合交付说明](docs/trade-minute-aggregation-2026-11-03.md)。

- **可能漏数或重复计数**：分页边界、断线、重复交易 ID、迟到或源端修订都可能影响聚合。
  必须验证分页推进、按合约与交易 ID 去重，并对每个已结束分钟核对 K 线成交量。
- **总量正确但买卖方向错误仍可能存在**：买卖方向反转不会改变总成交量。总量校验不能证明
  CVD 正确；需要独立方向样例、分类复算与历史抽查。测试通过也不等于所有源数据永远正确。
- **历史补抓有边界**：当前官方历史逐笔接口提供最近三个月分页查询；本地明细过期后可在
  接口可用范围内补抓。K 线只有总量，不能反推买卖分配；超出回溯范围不保证能恢复明细。
- **冷启动和预算不足**：完整 CVD 需要最近 20 个周期的连续成交，12h 指标对应 10 天。
  分钟累计未覆盖、补抓未完成或校验失败必须明确缺失/部分覆盖，不能自动退回已知有问题的
  统计接口或按 K 线差额猜买卖方向。补抓的新版本不得改写旧预测冻结事实。

这些是开发阶段需持续验证的风险，不代表上述错误都已发生；已确认的源端问题与核查证据见
[CVD 差异调查](docs/cvd-volume-investigation-2026-10-31.md)。

## 可能存在的问题：预测评分的分钟内先后顺序

预测发布时已确定目标和失效条件。情景已经触发后，若同一根1分钟K线内既触达目标又满足
失效条件，当前程序统一记为 `AMBIGUOUS`，目标概率评分留空。“同一分钟”不表示精确同时发生。

- **触价失效与收盘确认失效需要区分**：触价失效时，分钟OHLC可能无法确定目标和失效的
  先后，应在确实无法判定时保留歧义。收盘确认失效则须按条件的确认时点判断，不能一律按
  同分钟歧义处理。例如，已触发的看涨情景目标105、失效条件为1分钟收盘跌破95；随后某分钟
  最高105、收盘94，目标触达先于收盘失效确认，这种情况不应仅因发生在同一分钟就判顺序未知。
- **现有测试预期也需要审查**：[评分逻辑](trading-agent/src/trading_agent/prediction/evaluation.py)
  当前未区分上述情况，[对应测试](trading-agent/tests/unit/test_structure_predictions.py)还将
  收盘跌破与目标同分钟出现预期为歧义。测试通过只证明实现符合当前预期，不能证明该预期
  符合业务规则；后续应分别验证触价、收盘确认、可判先后及不可判先后的情形。
- **发生频率与影响尚未统计**：较大的目标/失效线间距可能使这类情况较少出现，实际仍取决于
  百分比间距和当时波动，不能仅凭BTC/ETH的绝对币价认定只在黑天鹅时发生。应统计已发布
  预测中的出现次数和评分影响，再评估修复优先级；本节不代表正式库已确认存在误评分。

本次仅登记待核查的评分问题，尚未修复业务逻辑或测试，不改写已发布预测及历史评分。

## 安装和检查

需要 Python 3.11、uv；数据库集成使用独立 PostgreSQL 16，Hermes/Cube 接入另行配置。

```sh
cd trading-agent
uv sync --frozen --extra cube --group hermes-test
uv run --frozen --extra cube --group hermes-test ta --config config.yaml config check --json
uv run --frozen --extra cube --group hermes-test pytest -q tests/unit
```

`config check` 不启动服务；仓库示例配置不代表部署已经验收。
默认关闭执行。原 `trade-agent cycle/daemon` 已退役，不再安装旧 CLI 或旧服务模板。
已有机器上的旧安装不会被本次代码清理自动卸载；切换前核对旧服务已停止。
正式迁移、角色身份、Hermes、Cube 隔离证明、模型和通知配置见
[部署说明](trading-agent/docs/deployment.md)。不得仅凭本地测试启动实盘。公开示例保持暂停，关闭执行、私有读取与通知；
Hermes、Cube和专用数据库须自行提供，示例配置检查可能明确报告配置未完成。

## 当前文档

- [主控 Gateway 与预测 Hermes 链路恢复](docs/controller-hermes-restore-2026-10-22.md)
- [旧测试与日志清理、当前验证](docs/obsolete-research-cleanup-2026-10-22.md)
- [项目结构、迁移与回滚](docs/runtime-core-consolidation-2026-10-22.md)
- [预测驱动交易：实现与使用](docs/prediction-driven-implementation.md)
- [需求规格](docs/prediction-driven-trading-goal.md)、[技术决策](docs/prediction-driven-decisions.md)、[验证边界](docs/prediction-driven-validation.md)
- [只读行情与后续工作](docs/readonly-data-and-next-steps-2026-10-22.md)
- [本机模拟数据库与身份](docs/local-paper-database-and-identity-2026-10-22.md)
- [历史资料索引](archive/README.md)

本机完整模型预测/模拟交易的验收状态以具体交付记录为准；行情、数据库、组件测试和
真实模型效果分别报告。历史文档中的实盘、策略自动改代码或旧启动命令不构成当前操作授权。

共享交易核心的回归在根目录使用独立开发环境：

```sh
cd ..  # 从trading-agent返回仓库根目录
uv sync --frozen --python 3.11 --extra dev
uv run --frozen --extra dev pytest -q
```

旧研究和旧交易入口测试已移除；仍适用的风控、执行、记账与量化检查直接测试共享库。
开发和生产依赖均不再包含 pandas。

## 公开范围与更多说明

本仓库保留当前框架、完整需求/设计/使用文档、测试样例目录和退役源码参考。
个人账户、真实预测/交易/复盘原文、凭据和部署证据留在仓库外；历史文档为脱敏副本，不能照搬为启动授权。

- [全部当前测试样例](docs/test-case-catalog-2026-11-04.md)
- [架构概览](docs/architecture.md)、[独立部署准备](docs/deployment.md)、[测试方法](docs/testing.md)
- [已知限制](docs/limitations.md)、[公开范围与逐项处理](docs/publication-scope.md)
- [维护与自动同步](docs/automatic-sync.md)

本仓库尚未指定许可证。
