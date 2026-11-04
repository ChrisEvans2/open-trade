> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

> 2026-10-22：已收敛为唯一结构预测 runtime 与 trading-core/trading-quant 两个共享库；旧入口和旧流程见 [归档说明](../archive/README.md)。

# 预测驱动交易：完整实现与使用

对应需求：[Goal规格](prediction-driven-trading-goal.md)。本次交付为本地代码与测试；线上部署、真实模型效果与真实订单验收另列于[验证与限制](prediction-driven-validation.md)。

## 1. 完整调用链

`行情 → 80根量化与结构候选 → 知识检索 → 独立周期报告 → 父Agent综合 → 交易意图 → 原风控 → 持久化执行 → 分钟事件验证 → 每日复盘 → 知识版本/期限学习`。

实现位于本仓库的 `trading-agent`、`packages/trading-quant` 和 `packages/trading-core`。新 runtime 通过本地 Python 包依赖复用共享交易核心，不启动第二个交易服务。BTCUSDT、ETHUSDT 对应 BTC-USDT-SWAP、ETH-USDT-SWAP；每币种维护 1h、4h、6h、12h 子预测及一个 1h 尺度的综合预测。

## 2. 行情、结构和输入证据

- `trading-agent/src/trading_agent/market/okx.py` 收集公开行情，支持明确的合约作用域。分钟补齐从最老缺口开始，每页300分钟、每次最多3页，游标只跨过连续、已收盘且已可用的数据。
- `prediction/market.py` 负责收集及冻结；`prediction/service.py` 保存80根计算历史、30根模型可见K线、全部来源ID、外部指标ID、特征、结构、期限规则和父子上下文。
- 每币种的四个周期预测视图合计**30×4=120根**，后台计算历史合计**80×4=320根**；复盘Agent读取四周期各80根、合计320根。BTC、ETH分别冻结。2026-10-31起父默认读取有效子报告、数据时点和子报告边界，不另采自身1h行情；需要时按child_ref回看该子报告的冻结证据。各子模型另收1根未收盘快照和80+1计算的临时指标，具体见[父综合输入调整](parent-synthesis-inputs-2026-10-31.md)。
- 结构算法在 `packages/trading-quant/src/trading_quant/structures.py`。每个极值需左右各2根已收盘K线确认；只使用当时已确认关键点，几何候选使用最近60根范围，ATR使用80根输入中的Wilder ATR14。
- 输出箱体、上升三角、收敛三角、W底的候选，可同时出现多个；无法成立则UNKNOWN。记录形成、突破、确认、回踩、失败突破、失效等阶段，水平/倾斜上下边界及关键点、测量高度、证据时点和版本。
- 默认容差为该周期ATR的0.25倍。模型选择候选、解释证据；服务器验证候选ID、类型、阶段、边界、斜率、容差及来源引用。UNKNOWN可引用窗口极值和最近收盘水平，不能编造已确认结构。
- 结构名称本身不构成交易准入规则。W底/上升三角不会自动产生做多订单。

## 3. 周期预测与父Agent

`prediction/contracts.py` 的新报告schema_version=3，API版本为 `/v2`；旧schema_version=2预测和历史接口保留。

子报告包含：symbol/timeframe、structure_id/structure/stage、primary_direction、p_up/p_down/p_none、scenarios、horizon_rule_id/horizon_bars、evidence_ids及reason。每个scenario包含方向、触发、失效、目标区间、目标条件概率和边界行为预期。

方向概率对应原观察窗口中首先确认UP、首先确认DOWN、均不确认，三者和为1。目标概率是相应情景触发后、原截止时间内、失效前触达目标的条件概率。它们是模型估计值，不自动标为历史校准概率。

服务器生成不可由模型指定的prediction ID、发布时间、截止时间、输入快照、引擎版本和替代关系。`PriceCondition` 支持 CLOSE_ABOVE/CLOSE_BELOW/TOUCH/SUPPORT/RESISTANCE、水平或倾斜PriceLine、固定容差、连续1～5根分钟确认；自然语言不能替代这些验证条件。

父报告另包含OPEN_LONG/OPEN_SHORT/HOLD/CLOSE、TREND/RANGE/BREAKOUT风格、选择方向、全部有效子预测引用、综合解释、上下文版本和当前持仓ID。父Agent给出自己的情景和概率；不照抄1h、不机械平均、不要求原技术策略同向、不增加概率准入门槛。缺失周期明确列出；完全没有有效子预测时不能开仓。

`prompts/structural/` 的六份提示词及原生Hermes插件驱动实际模型工作。独立周期任务只委派本次指定周期；父综合不重新委派四个周期。父子Agent都能检索本任务授权的知识；只有复盘任务能提出知识变更。

## 4. 独立生命周期与评分

`PredictionScheduler` 在现有单runtime内运行：启动补齐8个子周期；此后结束、失效、到期或结构阶段变化才重新分析对应周期。程序每分钟验证，模型无需每分钟/每小时全量重算。

任一周期发布或有效性变化会触发同币种父综合；尚未冻结输入的排队任务合并。冻结后的父任务发布时重验子引用、持仓和版本，过时则拒绝并排新任务。其它仍有效的子预测复用，不等待所有周期完成。

`prediction/evaluation.py` 逐分钟处理，验证从发布后的第一个完整分钟开始，只用截止前的完整分钟。连续确认数量是分钟K线数量。收盘才触发的情景，不拿同分钟更早的最高/最低证明触发后到达目标。

- 首次方向、目标结果、边界支撑/受阻/突破行为及耗时分别保存。
- 先失效后达标仍失败；同分钟目标/失效顺序不可确定则AMBIGUOUS。
- 未触发、已触发到期未决、无法判定和数据不足分别保留。数据缺口不补造，不删除不利样本。
- 已结束预测退出当前决策，原方向概率观察窗口仍继续采集，历史事实不改写。
- 复盘统计包含分桶命中率、Brier评分、结果分布、边界与时间数据，并标明未评分数量。条件目标概率只在可评分的已触发样本中评估。

## 5. 知识库与复盘

PostgreSQL是知识和案例的正式来源；SQLite/FTS5仅为可重建检索投影。

- **summary**：知识点、范围、结构阶段、适用条件、局限、证据状态。
- **content**：知识正文及关联案例的预测原件、输入证据、情景与验证过程；支持正例、反例、纠错、未决。
- **procedure**：预测/复盘应执行的检查步骤。

一个案例可以关联多个知识点，一个知识点可以引用多个案例。初始4份操作指引标为UNVERIFIED，不伪造成功历史。

搜索先限定任务币种/周期与决策时点，再按结构、阶段和关键词查询；默认最多5张卡，正文默认6000字符分页、最大12000。中文用双字分词，英文/数字按词分词。初始检索随任务上下文展示，模型可继续定向搜索和展开。

先取决策时点每个知识点的最新版本，再过滤退役/范围，避免旧条目复活。检索结果和降级状态写成不可变recall；预测只能引用任务已召回的版本。以后修订、合并、退役、重建索引不改变旧预测用过的内容。

每日UTC00:20后分别排BTC/ETH复盘任务。复盘上下文包含最近90天、至多200条验证案例及程序统计，以及本币种1h/4h/6h/12h各80根最近已收盘K线，共320根；模型提交NEW/REVISE/MERGE/RETIRE建议。程序验证证据范围、版本父链、正反例状态，在同一任务事务追加PG版本，再排可重试KNOWLEDGE_INDEX任务。

复盘行情由`PredictionMarket.prepare()`在事务外并发采集，按采集开始时的UTC整点锚定四周期，`KnowledgeService.freeze_review()`先完整检查根数、连续性、币种、边界和可用时点，再在同一任务事务保存来源revision及`review_market`。字段`bars_by_timeframe`含全部320根，`anchor_at/as_of`区分行情锚点和冻结时间，`purpose=REVIEW_ONLY`标记事后背景；API和沙箱使用同一冻结输入。缺数据则失败重试，不伪造或截短；已冻结输入不因重试或行情修订而改变。它不替代原预测证据或超出当前窗口的历史案例验证，也不重写历史评分。

索引缺失、落后、损坏或锁冲突时，本次检索降级到PG同版本有限摘要匹配，并冻结该事实。索引修复不会回写旧recall。重建用 `KnowledgeService.rebuild()`；损坏文件应在停用索引写入后保留备份、移走SQLite主文件及对应WAL/SHM，再重建。事实始终在PG，无需重跑模型来恢复知识。

## 6. 期限学习

`prediction/horizons.py` 按币种/周期/结构/阶段学习；初始4根周期K线，父综合初始4小时。同组1h规则供父/子1h报告引用；学习样本只取独立周期预测，避免父子重复计样本。

候选为2/3/4/6/8/12根。每次只改变反事实预测的期限，保留原结构、触发、目标、失效线及其它证据。至少40个可完成配对回放的样本才创建候选；再积累候选提出后发布的20个后续影子样本，未够数量不生效。

效用：达标1、失效−1、未决0，减去等待周期数×0.01。保留达标率、失效率、未决率、未触发率和等待时间。配对平均效用提升≥0.02且失效率增加≤0.02才追加新规则版本。缺失/无法判定样本不计入有效门槛，但预测及验证事实保留。

生效后仍比较旧规则，每20个完整新样本复查；平均效用恶化超过0.02或失效率增加超过0.02时追加回滚规则。历史报告、旧期限与评分不变。历史扫描每组最多200份预测，程序小时级学习，不额外调用模型。

## 7. 风控、持仓和订单执行

`packages/trading-core/src/trading_core/strategy/risk_parameters.py` 抽取原趋势ATR止损、收益风险倍率、突破仓位和区间边界止损/中点止盈公式。归档旧策略回归仍引用同一函数。`trading_core/prediction_adapter.py` 将父意图转换为StrategySignal直接交原RiskEngine，复用动态仓位/杠杆、最大可承受损失、账户级限制、移动止盈及区间退出；不调用旧方向信号或Advisor裁决。

父选择RANGE必须提供已验证箱体，当前价格和中点必须有正向收益空间；这是风险参数有效性校验。预测目标单独保存，不能替换实际订单止盈。

- 父预测失效或转向：绑定当前position_id及revision的CLOSE；先平仓，后续新综合才能反手。
- 子周期反向：只触发父综合。
- 单纯到期：保留持仓及既有止损止盈；原结构继续按独立持仓游标检查，补齐遗漏分钟后也不会漏掉已经发生又恢复的失效。新同向综合可接管结构监测。
- 无持仓不生成平仓订单；旧版本平仓意图不能关闭后来新开的仓位。

`trading-agent/src/trading_agent/execution/` 保存trade_intents、trade_intent_states、execution_events、账户和持仓投影。客户端订单ID由意图ID哈希派生，最长32位。

执行先检查当前父/子验证进度、分钟行情、预测版本、账户和持仓；外部风险信息及合约查询在DB事务外，提交前再核验。先持久化PREPARED/SENDING才发送。SENDING之后重启或响应未知只查询该订单，查不到也不盲目重发。

累计成交量转为增量入账；重复查询不重复增加仓位/PnL；部分平仓保留剩余数量。费用单独修订也会进入原持仓累计盈亏；分批平仓/撤余单只有在整个持仓平完后才累计一次输赢，避免提前触发连续亏损限制。账户内未决订单先处理完，BTC/ETH不会按未占用资金重复定仓。若开仓剩余部分未成交而预测结束或出现绑定平仓要求，查询确认未结后撤销剩余数量；到期不关闭已经成交的仓位。

模拟成交由冻结订单确定，重启可重现；未引入随机滑点/手续费模拟。实盘transport复用旧OKX客户端、数量算法及风险参数，关闭数量使用实际剩余合约张数。固定止损止盈随开仓请求附带；运行时继续执行基于实际成交价的原移动/区间退出。附带保护价初始基于请求时价格，成交价差异需要在线验证。

交易所自主退出通过只读权益/持仓/历史仓位核对，绑定交易所position ID及唯一成交历史，以真实realizedPnl记账；证据缺失不使用标记价伪造退出。账本外持仓、数量/身份冲突会阻止新提交并记录任务错误。

账户硬风控（当日亏损、最大回撤、连续亏损或健康检查失败）形成PG持久暂停标记，重启和切换币种不会绕开；平仓仍可执行。只有既有operator权限的 `POST /v1/schedule/resume` 能清除该标记，恢复操作保留原审计和幂等；下一次交易仍重新检查实际风险，不会因恢复操作豁免限制。仅需恢复分析调度时，operator可使用 `POST /v1/schedule/resume?preserve_risk_halts=true`，原账户熔断内容与账户修订号均保持；默认调用继续沿用原来的清除行为。两种操作的幂等请求身份不同，同一幂等键不能混用。每次准备订单与监控读取现有运行/风险/策略配置，发送前检查准备时参数是否仍相同；执行模式变化需要重启验证。

## 8. API、CLI和运行方式

默认 `prediction.mode: structural`、`execution_enabled: false`。本次没有改现有实盘配置或启动正式runtime。示例：

```yaml
prediction:
  mode: structural
  symbols: [BTCUSDT, ETHUSDT]
  timeframes: [1h, 4h, 6h, 12h]
  initial_horizon_bars: 4
  tolerance_atr: '0.25'
  knowledge_index_path: .local/private-evidence
  execution_enabled: false
  legacy_config_dir: ../config
  account_key: prediction-main
```

从仓库 `trading-agent` 目录使用本地Python3.11环境；`uv sync --group hermes-test --extra cube` 安装含两个本地包的依赖。应用新增0009迁移后，既有部署校验/锁/资源/凭据流程仍适用，`ta serve` 拥有一个runtime、四个确定性任务槽和一个模型父进程槽。迁移前备份数据库；不通过回滚迁移删除事实。

研究任务API：`GET /v2/research/context`、`POST /v2/research/prediction`、`POST /v2/research/decision`、`POST /v2/research/knowledge/search`、`GET /v2/research/knowledge/content`、`POST /v2/research/review`、usage。全部受任务授权和期限保护；没有供模型调用的执行HTTP入口。

控制侧查询：

```bash
ta --config config.yaml --json prediction latest --symbol BTCUSDT
ta --config config.yaml --json prediction latest --symbol ETHUSDT --timeframe 12h --child
ta --config config.yaml --json prediction show <prediction-uuid>
ta --config config.yaml --json intent show <intent-uuid>
```

对应GET `/v2/predictions/latest`、`/v2/predictions/{id}`、`/v2/intents/{id}`。Hermes bridge新增trading_prediction_latest/get、trading_intent_get；旧forecast/history工具继续可用。

## 9. 迁移与恢复边界

旧交易CLI（已归档）保留兼容使用；**同账户禁止旧CLI与预测runtime同时执行**。执行开关关闭时只保存意图，状态DISABLED的历史意图不会在以后启用时补发。

首次启用执行需要完成旧持仓/订单迁移核对；当前自动切换要求账户平仓且无未决订单。运行中切换模型/提示词/预测规则导致engine版本变化时，若还有持仓或未决订单，会拒绝切换，先由原版本完成处理。此限制没有改变已有运行状态。

历史迭代代码模块和证据保留用于历史读取；旧iterate与旧经验索引CLI/插件入口移除，旧提示词转至 `trading-agent/docs/archive/legacy-iteration.md`。当前AGENTS职责改为预测、知识复盘和确定性期限验证。

## 10. 代码包、授权与恢复补充

三个Python wheel分别封装旧风控、共享量化和新runtime。runtime wheel包含六份结构提示词、研究/控制/历史兼容插件及0009迁移；沙箱输入白名单包含三份预测/决策/复盘JSON Schema，继续拒绝未知宿主文件与符号链接。结构预测与复盘只使用冻结数据/API授权知识，沙箱不开放外网。模型调用预算沿用单任务120秒；历史任务接口与旧数据语义继续保留。

部署锁与runtime共用结构模式engine hash，包含模型、提示词、预测配置及证据契约；索引文件路径、执行开关、风险配置目录和账户名称不改变预测含义，因此不进入预测engine标识。知识复盘已提交但进程尚未记录成功时，恢复任务会查询原提交并标成功，不重复追加知识。

SQLite重建是维护入口 `KnowledgeService.rebuild()`，本次没有新增对模型开放的重建/执行/解除风控工具。CLI和Hermes控制插件只读查询新预测与交易意图；兼容v1/v2控制地址。订单观察拒绝非有限数值、无关订单/合约和不能换算的非USDT费用，不能确认的结果留待核对。
