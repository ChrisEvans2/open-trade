> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 交易 Agent v1：技术细节与实施依据

> 2026-10-11 同步版；与[实施设计](交易Agent-v1-实施设计.md)共同描述当前代码。需求：[Goal规格](../../../docs/prediction-driven-trading-goal.md)。本文件说明实现方法、初始参数、自主配套和限制；原文见[同步前快照](../../../archive/legacy-research/docs/prior-specifications/specifications-before-prediction-sync-2026-10-11/README.md)。
> 已完成本地实现与回归；没有部署或调用真实订单。模型和订单测试使用fixture，真实PostgreSQL参与集成测试。本文的已实现不等同于线上预测有效或可盈利。

## 先读这一页

| 技术主线 | 要回答的问题 |
|---|---|
| 一、行情、结构与验证 | 怎么识别结构、定义预测、独立更新和公平评分？ |
| 二、后端可靠性 | 怎么防止旧任务写入、丢失事实、崩溃重做和重复下单？ |
| 三、沙箱、权限与接口 | 模型能读什么、提交什么；确定性执行怎样隔离？ |
| 四、知识题库与期限学习 | 怎么从知识点找到案例，如何学习、验证、回滚？ |
| 五、原风控与订单执行 | 怎样保留交易规则，并处理失效/到期/部分成交？ |
| 六、自主配套与验证边界 | 哪些是开发时作出的决定，哪些仍需在线验收？ |

当前链路：**冻结行情/结构 → 独立周期预测 → 父综合/意图 → 原风控/执行 → 分钟事件验证 → 每日知识复盘及期限规则更新**。旧小时分类标签与策略迭代记忆只作为历史版本保存。

## 一、行情、结构与验证

### 1.1 输入、模块和时间

实际模块在 `trading-agent/src/trading_agent/prediction/`、`knowledge/`、`execution/`；结构与指标在仓库的 `packages/trading-quant/src/trading_quant/`。复用旧框架通过本地包依赖实现，没有第二个交易服务。

| 输入 | 规则 |
|---|---|
| symbol/inst_id | BTCUSDT/BTC-USDT-SWAP、ETHUSDT/ETH-USDT-SWAP严格匹配 |
| 周期 | 各自1h、4h、6h、12h；父综合报告必须1h |
| 计算历史 | 每币种每周期80根连续已收盘K线，四周期共320根，保留原始revision/ID |
| 预测模型视图 | 每周期取上述80根中的最近30根，四周期共120根；特征/结构仍来自完整计算历史 |
| 复盘模型视图 | 每币种每周期最近80根已收盘K线，四周期共320根；与历史预测案例和评分分开提供 |
| 时间 | UTC及带时区字段，展示Asia/Shanghai；特征只用冻结时已可用数据 |
| 边界 | 1h→1H、4h→4H、6h→6Hutc、12h→12Hutc；按UTC周期边界对齐 |
| 质量 | 有限正价格、非负成交量、OHLC关系、范围/顺序/连续性/最新收盘边界 |
| 时钟 | 默认允许偏差最多5秒；行情请求受超时、重试、流控和任务截止限制 |

`PredictionMarket.prepare()`先核对engine和已有冻结输入；没有输入才采集本周期K线及观测，调用`PredictionService.freeze()`，再做一次知识初始检索。重试沿用已冻结输入。输入包含input ID、行情ID、30根视图、指标、候选、期限规则、有效子预测、持仓及context_revision。

上下文默认最长180秒；超时或引用改变时拒绝旧报告，另排新任务。预测截止由发布时刻加所属周期根数计算，发布后的首个完整分钟为observation_start。旧T+1/T+4/T+5小时预测时间表不用于结构模式。

### 1.2 指标与缺失数据

共享指标版本为 `ohlcv_wilder_v1`；当前必需项是return_1、return_20、atr_pct、rsi，其余按可选特征及质量状态处理。

| 类别 | 当前计算/证据口径 |
|---|---|
| 价格 | 1/3/5/10/20根收益、单根log return；ROC复用收益口径 |
| 趋势 | Wilder14 ADX/+DI/−DI；20根回归斜率/R²、效率比；14根Choppiness |
| 动量/波动 | Wilder14 RSI/ATR%、收益加速度、20根布林宽度；10根波动对60基线做Z-score |
| 成交量 | 当前量对前20根的RVOL/Z-score；同单位成交额/币量的VWAP偏离 |
| 结构位置 | 前20根高低距离、ATR归一突破强度、收盘在当根区间的位置 |
| 衍生品 | CVD/Taker、OI变化、当时资金费/30次结算事件基线、多空比、公开强平采样 |
| 市场背景 | BTC相关性/Beta/相对强弱及固定篮子宽度，依赖实际可用参考行情/观测 |

数值统计参数锁定在配置/版本中：总体标准差ddof=0、平价RSI=50；缺历史、零分母或覆盖不足输出质量及原因。BTC与自身比较标NOT_APPLICABLE；ETH相关指标需要BTC参考数据，不能把“支持该指标”写成“每次都有完整实测数据”。未配置市场篮子不编造成分和宽度。

CVD不能由K线涨跌替代；OI不把USD估值当合约数量；资金费公布信息与实际结算分开；公开强平只代表采样覆盖。保留source/event_at/available_at/quality等证据，禁止未来插值、null填0和假装缺失指标已经接通。更完整的既有量化说明见[共享量化模块](../../../archive/legacy-trading/docs/v1-quantitative-module.md)。

### 1.3 结构算法

`structures.py`返回不可变Pivot、Boundary、StructureCandidate。原始输入不足80根、混合币种/周期、不连续或包含未来可用数据会拒绝计算。

局部极值使用左右各2根确认：左侧要求严格更高/低，右侧允许相等，平台取首个极值；记录极值时刻和确认可用时刻，不能提前使用尚未确认的拐点。最近60根内的确认点参与候选几何。

ATR由连续TR以Wilder14平滑，容差为ATR×0.25。边界表示为：

```text
line(t) = price + slope_per_second × (t − at)
```

| 候选 | 当前几何条件 |
|---|---|
| BOX | 最近两个高点近似水平、两个低点近似水平；差值各在容差内 |
| ASCENDING_TRIANGLE | 两高点近似水平、第二低点抬升超过容差；上边界水平、下边界倾斜 |
| CONVERGING_TRIANGLE | 高点下降、低点上升且各超过容差；上下边界倾斜 |
| W_BOTTOM | 最近两低点间隔5～40根、差值≤2倍容差；中间存在确认高点作为颈线，颈线高出两底至少1ATR |

候选当前宽度必须大于2倍容差，下边界为正；已经交叉的收敛边界不保留。可同时返回多个形态；空候选对应UNKNOWN。候选hash包括类型、阶段、点、线、容差、ATR、观察时点和结构版本。

阶段由最近12根已收盘K线相对边界判断：FORMING、BREAKOUT_UP/DOWN、CONFIRMED_UP/DOWN、RETEST_UP/DOWN、FAILED_BREAKOUT；W底跌破底边界可为INVALIDATED。连续越界收盘与接触后收回的行为决定确认/回踩等阶段。这是明确的初始几何规则，不能宣称为通用形态识别器或已经历史验证的最优参数。

Agent选候选并解释证据；程序核验structure_id、类型、阶段、引用点/线、斜率、容差。UNKNOWN只允许冻结回退线（窗口极值/最近收盘），不能以自由文本造出新形态边界。

### 1.4 预测字段与概率含义

`PredictionReport`、`PublishedPrediction`、`ParentDecision`均使用严格Pydantic契约，禁止未知字段和非有限数值。新报告schema_version=3，API版本独立为v2。

| 对象 | 主要字段及约束 |
|---|---|
| PredictionReport | symbol、timeframe、structure_id、structure、stage、horizon_rule_id/horizon_bars、primary_direction、p_up/down/none、scenarios、reason/risk_notes、evidence_ids、knowledge_version_ids |
| Scenario | direction、trigger、invalidation、target.low/high、target_probability、至多4个boundary_expectations |
| PriceCondition | behavior、line.at/price/slope_per_second、tolerance、confirmation_bars（1～5分钟） |
| PublishedPrediction | 服务端分配id/engine_id/snapshot_id、inst_id、role、published_at/expires_at/observation_start、reference_price、supersedes_id |
| ParentDecision | prediction、action、trade_style、selected_direction、全部有效child_prediction_ids、context_revision、position_id、relationship |

每个方向至多一个scenario；p_up>0必须有上行情景，p_down>0必须有下行情景；三概率和为1（校验容差0.000001）。primary_direction如果存在，必须有对应情景。开多/空的selected_direction、primary_direction和情景必须一致，父报告固定1h尺度。

- 方向概率：原截止前首先确认上行、首先确认下行、均未确认；不是下一根K线红绿分类。
- 目标概率：相应情景触发后、原截止前、失效前触达区间的条件概率，不与方向概率混为一个数。
- 上涨目标区间高于发布时触发边界，下跌目标低于它；low≤high，整个期限的条件边界均须为正。
- knowledge_version_ids只能来自当前任务已冻结的召回；市场evidence_ids只能来自授权输入。
- horizon_rule_id和根数须匹配本币种/周期/结构/阶段的冻结规则，模型不能延长旧预测。

### 1.5 可执行条件与事件先后

逐分钟以该分钟收盘边界计算倾斜线价格L，容差为T：

| behavior | 判定 |
|---|---|
| CLOSE_ABOVE | close > L+T |
| CLOSE_BELOW | close < L−T |
| TOUCH | 分钟区间与[L−T,L+T]相交 |
| SUPPORT | 接触容差带且close > L+T |
| RESISTANCE | 接触容差带且close < L−T |

confirmation_bars要求连续、同合约、已闭合分钟满足条件。上行情景只用CLOSE_ABOVE/SUPPORT触发，下行情景只用CLOSE_BELOW/RESISTANCE触发，失效条件必须表达对应方向被破坏。

情景状态为WAITING、ACTIVE、ACHIEVED、INVALIDATED、AMBIGUOUS、EXPIRED。预测lifecycle与各情景状态分别存储，first_direction也单独保存。遇以下情况：

1. 失效先发生，后来达标：原情景保持INVALIDATED。
2. 同一分钟触及目标和失效且无顺序证据：AMBIGUOUS，不猜有利顺序。
3. 收盘才确认触发：不能拿该分钟更早的高/低点作为触发后的达标。
4. 截止前未触发：保留未触发信息；触发但未完成也单独可辨。
5. 数据缺口：data_quality=AWAITING_DATA、保留missing_at，游标停止，不假定无事件。
6. 预测生命周期已结束：停止供当前决策使用，原观察窗口仍继续采集评分所需数据，不复活旧预测。

### 1.6 评分与校准数据

`evaluation.scores()`记录方向label/Brier、情景outcome、triggered_at/terminal_at、elapsed_seconds、highest/lowest、target_distance和boundary_counts。边界行为包括tested/held/failed等计数。

```text
方向 Brier = Σ (p_类别 − 该类别是否发生)^2
目标 Brier = (p_目标 − 是否达标)^2
```

目标Brier只用于已触发且结果可判定的情景；未触发/无法判定不伪造为达标或失败。`knowledge.service.calibration()`按0.1概率分桶，提供n、hits、hit_rate、mean_brier、unscored，同时保留所有生命周期结果及数据质量计数，概率状态仍为MODEL_ESTIMATE。

历史最高/最低及目标距离可供复盘检查空间误差；本次没有另建训练概率映射器或保证区间覆盖率的统计模型。不能把“提供校准统计”说成“已经获得校准后概率”。

### 1.7 独立生命周期与父综合

`PredictionScheduler`启动补齐2币种×4周期，程序按分钟验证。达标/失效/到期退出当前head，阶段变化排本周期分析。阶段检查依赖完整有效的80根周期输入，缺口未补齐时不以过时状态触发开仓。

一个周期更新即可排PREDICT_PARENT；父上下文仅含同币种仍ACTIVE、未到期且质量合格的子预测，并明确missing_timeframes。程序不规定机械权重，父给独立关系解释/概率/目标/意图。

父提交必须引用冻结的全部有效子预测；发布前重算context_revision及head代际，校验持仓绑定。纯监控游标推进不改变语义hash；子预测、持仓身份或结构失效改变会拒绝过时决策。排队但未冻结的同币种父任务可以合并；运行中的旧任务不能覆盖新状态。

分钟取数每页300根、每次最多3页，从最老缺口开始。stream.cursor与各预测verified_until分别管理；执行时同时检查父/子验证进度，避免行情已写入但尚未验证的竞态。

## 二、后端可靠性

### 2.1 单runtime、任务与事务

一个Uvicorn worker装配API、调度、任务执行、Prediction/Knowledge/Horizon/ExecutionService、AgentRunner、notifier和recovery。领域模块直接调用，不互相绕HTTP；PostgreSQL独立作为数据库服务。

| 循环/限制 | 当前值 |
|---|---|
| scheduler / executor / notifier / recovery | 60 / 2 / 5 / 15秒 |
| 模型顶层并发 / 确定性并发 | 1 / 4 |
| lease / heartbeat | 60 / 15秒 |
| DB语句超时 / 锁等待超时 | 5000 / 2000毫秒 |
| 关闭等待 / 进程终止宽限 | 20 / 3秒 |
| 模型首轮 / 总预算 | 90 / 120秒，另受任务截止约束 |

新任务包括PREDICT_TIMEFRAME/PARENT/VERIFY/REVIEW/EXECUTE、KNOWLEDGE_INDEX、HORIZON_LEARN。旧SETTLE/PRICE_REFRESH可继续处理原事实；结构模式不再启动旧FORECAST主流程。

`jobs`是任务来源，`job_attempts`保存每次领取。用`FOR UPDATE SKIP LOCKED`在短事务领取，增加fence，事务外调用模型/行情/交易所，再进入短事务校验并写入结果。request幂等键、任务dedupe_key及事实唯一键分别保护不同层次。

### 2.2 租约、权限和事实不可变

任务写入必须在同一事务锁定并确认attempt、owner、fence、状态、lease、token和deadline。过期租约不能被迟到心跳复活；新的接管增加fence，旧执行者即使返回合法JSON也不能写。

生产以PG实际时钟检查，等待锁之后重新读时间。测试时钟注入同一时间来源，包括attempt.started_at，避免真实数据库时间与可控测试时间混用。

0009新增12类只追加事实：prediction_inputs/facts/evaluations/decisions、trade_intents/execution_events、knowledge_versions/case_links/recalls、horizon_rules/trials/comparisons。数据库拒绝UPDATE/DELETE/TRUNCATE。head/state/cursor/account/position等是受控可更新投影，不能据此覆写正式事实。

同币种内容锁串行化预测发布/验证，账户锁串行化执行账本；单实例PG所有权保护整个runtime。进程内锁负责本进程I/O顺序，不能代替PG资格检查。

### 2.3 恢复行为

| 已保存状态 | 恢复方式 |
|---|---|
| 正式预测已提交、job未标成功 | 查原prediction事实补完成，不重新调用模型 |
| 知识复盘已提交、job未标成功 | 通过reviews/attempt归属查原结果，不重复追加版本 |
| 输入冻结、任务仍可执行 | 重试沿用原输入；上下文过时则拒绝并排新任务 |
| 模型截止已过 | 记录失败/跳过原因，不补造历史预测 |
| 分钟数据缺失/停机 | 按原游标连续补齐，已有预测继续验证 |
| PG已提交知识、SQLite未同步 | 重试KNOWLEDGE_INDEX或维护重建；检索可降级PG |
| 订单处于SENDING/UNKNOWN/PARTIAL/WORKING | 查询稳定客户端ID，按累计回报处理；不重发 |
| engine改变且有仓或未决单 | 拒绝自动切换，由原版本处理完后再切换 |

状态沿用QUEUED、RUNNING、RETRY_WAIT、AWAITING_DATA、SUCCEEDED、FAILED、SKIPPED；新业务以其自身状态投影记录细节，不把旧FORECAST阶段名套到结构任务。

模型路径受限重试，确定性暂时失败按任务规则重排。未知订单可能长时间阻塞本账户后续提交，这是保守行为，应核对而不是删除记录解锁。

### 2.4 AgentRunner与资源恢复

使用最小环境、受管理进程组、PID及启动时间识别归属，输出限长并脱敏；不能只用PID误杀后来复用该PID的进程。超时撤销任务资格，终止整个所属进程组，并清理本attempt的远程沙箱。

退出码或stdout不成为成功证据，Runner查询PG正式预测/复盘。旧模型进程失去fence后不能提交。沙箱创建/清理都记录job/attempt/fence metadata；创建后未登记ID即崩溃时，按明确归属恢复；归属不明不删除别的资源。

模型供应商、Cube或数据库不可用不会触发宿主机终端兜底，也不会自动改用其他模型/行情来源。运行配置与实际隔离仍需部署验收。

### 2.5 通知、健康与日志

沿用outbox、claim_id和事务外投递；业务事实不依赖消息发送成功。系统启动及系统/账户类通知经主控Profile；配置独立预测目标后，预测Profile仅发送父综合摘要和错误告警。子周期结果、生命周期结束和分析恢复成功仅留后台记录；旧待发同类状态通知停发，SENT/UNKNOWN和正文保留。父摘要结论在前、按概率/期限/依据分段，错误说明原因/影响/重试计划；预测及事件编号不显示在正文，后台继续关联。主控请求的解释/复盘仍回复主控。通知可能已送达但结果未知，外部发送不承诺恰好一次；不另加逐分钟推送或专属日报模板。

healthz检查存活，readyz检查数据库/迁移、实例锁、后台循环及运行依赖。日志用request/job/attempt/fence、预测或意图ID关联；正常空轮询/续租避免刷屏，错误保留脱敏code及追踪信息。

运行日志、audit和业务事实分别负责排障、授权动作和正式证据。默认10,000,000字节轮转、10份备份、1000条队列；运行日志不重复打印模型全文或原始私有交易所响应。密钥、token、完整连接串、环境原文不进入日志。备份/恢复演练及真实故障告警仍需在线验收。

## 三、沙箱、权限与接口

### 3.1 执行边界

Hermes推理编排和受信任插件在CLI进程；模型终端命令进入Cube。宿主runtime执行正式数据、风控和订单工作。Profile负责配置/会话分离，不能被当成操作系统安全边界。

`trading-cube`按固定Hermes commit实现provider、命令、进程和清理；SDK目标0.7.0，模板/认证/网络需匹配部署锁。正式环境不可用则失败，不能降级为local终端。

input只读、scratch可写，不挂载宿主源码、HOME、Profile、数据库或管理socket。显式普通用户，限制CPU/内存/磁盘/进程/输出。当前默认2 CPU、2048MiB内存、4096MiB磁盘、128进程、1MiB输出；限额真实生效仍需Cube验证。

文件白名单包括context.json、task.txt、角色prompt、既有calculation文件及prediction-schema.json、decision-schema.json、review-schema.json；拒绝未知宿主文件、符号链接及越界路径，单文件/总输入限额继续生效。

### 3.2 网络与凭据

PREDICT_TIMEFRAME、PREDICT_PARENT、PREDICT_REVIEW使用冻结数据与授权知识，终端禁止出站。旧EXPLAIN/REVIEW_DAILY可按原研究策略开放受限公网，它们不是新预测复盘任务。模型供应商调用由受信任Hermes完成，不向guest发模型或交易所key。

Cube控制/数据通道、认证、IPv6、guest管理服务提权、文件同步竞态等仍要真实环境验收。固定版本原生插件测试只能证明接入契约，不能替代隔离报告。

| 身份 | 权限 |
|---|---|
| 周期/父任务 | 读取本任务输入及知识；发布本周期预测或父决策，按任务类型限制 |
| 原生周期子Agent | 读取授权知识、返回报告JSON；不能改知识、发布父决策或再委派 |
| 预测复盘任务 | 读取冻结案例和知识，提交知识变更建议 |
| controller | 查询预测/历史/意图及既有允许任务入口，无订单执行权限 |
| operator | 既有暂停/恢复等人工操作，包括解除持久风险暂停 |
| 确定性runtime | 验证、索引、学习、风控、执行；交易所凭据不发给模型 |

任务token不透明、服务端保存hash，绑定attempt/fence/任务范围/期限。多次读取允许，但不能携token指定任意job、as_of或另一币种。完成、超时、撤销或失租后失效；写入时再次检查。

### 3.3 API与工具限制

`/v2/research/context`由token定位输入，不接受自选范围参数。knowledge/search的query最多300字符、limit为1～5；content只允许version_id、offset、limit，并须属于已召回版本，默认6000字符、最高12000。

prediction/decision/review分别验证PredictionReport、ParentDecision、KnowledgeReview。新API和schema各自版本化；API v2不意味着旧schema2概率含义不变。

`forecast_submit`等旧/共用工具名的实际权限由任务决定，父综合使用decision_submit；timeframes_analyze对新周期任务只委派当前周期。knowledge_search/expand允许父子读取，但review_submit仅复盘可用。旧legacy_strategy_iterate、legacy_strategy_memory_search/read不可调用。

GET新预测和意图查询可由CLI/主控使用；控制插件接受v1/v2基础地址并选正确路由。执行没有模型HTTP端点，模型提交action只是意图，仍需程序风控和有效性检查。

## 四、知识题库与期限学习

### 4.1 正式数据模型

| 实体 | 作用 |
|---|---|
| concept_id | 同一知识点的稳定身份 |
| knowledge_versions | revision、前序版本、币种/周期/结构/阶段、title/summary/applicability/limitations/content/procedure/evidence_status及原因 |
| knowledge_case_links | 知识版本关联prediction_id+evaluation_id；多对多，关系SUPPORT/COUNTEREXAMPLE/UNRESOLVED/CORRECTION |
| knowledge_recalls | 当前input的查询hash、版本集合、摘要及降级情况；固定本次已看到的知识 |
| reviews/review_inputs | 每日复盘叙述、冻结案例/统计及提交归属 |
| SQLite versions/summaries | PG版本hash及正文投影、FTS5摘要索引；可重建，不是权限来源 |

证据状态为UNVERIFIED、HYPOTHESIS、EVIDENCE_SUPPORTED、REJECTED。声称EVIDENCE_SUPPORTED须有案例；支持案例不能指向失败事实，时间、币种和授权范围由程序核验。

初始种子针对四类形态的检查步骤，均未验证、没有虚构case。procedure属于知识版本的一部分，可随复盘修订；不是一个新的具有执行权的Agent。

### 4.2 搜索与展开

1. 从任务输入取冻结时点和币种/周期，选择该时点每个concept的最新版本。
2. 先处理退役，再过滤结构、阶段和适用范围，避免旧版本被筛选“复活”。PG候选最多500份。
3. SQLite只对PG允许的版本ID/hash排名；缺任一合格版本或hash不一致视为落后，不能悄悄漏掉知识。
4. FTS5索引title/summary及范围标签；中文双字、英文数字词，query词OR匹配、BM25排序。不是向量相似检索。
5. 默认返回最多5张summary卡，持久冻结recall；再以version_id分页展开content、procedure和关联案例证据。

模型每次预测都会看到初始检索，也可按结构/阶段继续定向搜索。搜索不到时应明确无证据，不从旧迭代日志自动造案例。后续知识更新、退役或索引重建不改变旧任务冻结的版本。

### 4.3 修改、同步与恢复

每天UTC00:20（北京时间08:20）后分别排BTC/ETH复盘；它是可延迟的低优先级任务，不保证此刻恰好运行。复盘冻结最近90天、至多200条案例及程序统计，同时提供该币种四周期各80根、合计320根已收盘K线。

`PredictionMarket.prepare()`为PREDICT_REVIEW在事务外并发获取四组公开行情，共用采集开始时的UTC整点anchor及40秒网络截止时间；`KnowledgeService.freeze_review()`在任务事务内校验四周期、根数、连续性、合约、收盘边界与available_at，然后追加来源revision并冻结到`prediction_inputs.body.review_market`。`bars_by_timeframe`保存四组完整K线，`anchor_at`为行情锚点，`as_of`为冻结时点，`purpose=REVIEW_ONLY`区分事后行情。研究API和沙箱context.json均读取该冻结内容，重试不重新取数。任一窗口不完整则失败重试，不补造、不静默降为30根。

三个配置字段分别为`market.calculation_bars_per_interval=80`、`agent_bars_per_interval=30`、`review_bars_per_interval=80`，均以Literal固定当前契约。新复盘使用320根，不修改已冻结的旧复盘。较早案例可能超出这320根覆盖范围，其结论仍依赖原预测/分钟事件事实；当前行情不能当作预测时已知证据。2026-10-31用户修订：父以CHILD_SYNTHESIS冻结有效子报告、来源输入ID/hash、数据时点、采用的结构及边界，不再采集或默认输入自己的1h行情/指标。research_context(child_ref=child.Nh)仅允许回读本次所引用子报告的原冻结输入；父输出仍以1h为期限单位。详见[交付说明](../../../docs/parent-synthesis-inputs-2026-10-31.md)。

`KnowledgeReview`每次最多10个change。NEW生成concept；REVISE/RETIRE要求准确前序版本；MERGE要求至少两个来源并生成新concept，同时退役来源。案例及版本父链与当前任务授权/时点一致才允许追加；并发过时修订拒绝。

PG事务提交知识及索引job后，事务外写SQLite，成功再完成索引任务。重试upsert同ID/hash幂等；若SQLite缺失、落后、锁冲突或损坏，检索降级PG同版本有限摘要，并记录degraded。PG事实不因SQLite失败回滚或消失。

`KnowledgeService.rebuild()`从PG重建派生内容；损坏SQLite文件需要暂停索引写入、保留故障副本并移走主文件/WAL/SHM后重建。维护重建不是模型工具，也没有承诺跨库分布式事务。

### 4.4 学习的分组、样本与评分

分组为engine/symbol/timeframe/structure/stage，默认4根，候选2/3/4/6/8/12根。父综合为1h尺度，使用该组1h规则；有效学习样本只取独立周期报告，避免同一市场事件的父子报告重复计数。

`horizons.replay_pair()`以原预测为基础，只替换反事实截止时间，固定结构、证据、目标、触发与失效条件。两条路径都需要完整可观察数据；缺口或AMBIGUOUS不进入有效样本门槛，但仍保存事实与unscored。

```text
utility = outcome_reward − waiting_cost_per_bar × elapsed_bars
outcome_reward：ACHIEVED=1，INVALIDATED=−1，其它未决=0
默认 waiting_cost_per_bar = 0.01
paired_gain = mean(candidate.utility − incumbent.utility)
```

同时输出达标率、失效率、未决率、未触发率和平均等待根数。至少40个完整历史样本回放后才能提出候选；再等候选产生之后发布的20个后续影子样本，不能用原历史样本冒充影子。

生效门槛：paired_gain≥0.02、失效率增加≤0.02；追加horizon_rules并推进head。生效后保留旧规则影子比较，每20个完整新样本复查；gain<−0.02或失效率增加>0.02则追加回滚规则。

候选、比较事实及状态分别在horizon_trials/comparisons/trial_states保存。小时级确定性任务扫描，每组最多200份原预测；样本不足维持初始值。程序不会改写已发布预测或历史评分，也不会修改风险上限。

## 五、原风控与订单执行

### 5.1 风险参数复用

旧`freqtrade_style`和新`PredictionRiskAdapter`共用 `src/trading_system/strategy/risk_parameters.py`。新入口跳过旧方向信号/Advisor，父意图转换为StrategySignal后直接交原RiskEngine。

| 风格 | 保留的参数计算 |
|---|---|
| TREND | stop=max(2×ATR%, atr_min_pct)，默认收益风险倍率2.0；原趋势建议仓位 |
| BREAKOUT | 同ATR止损，倍率1.5；原core_breakout_position_pct |
| RANGE | buffer=max(atr_min_pct, 0.5×ATR%)；多头止损在下沿外、空头在上沿外；目标为区间中点，原range_position_pct |

金额预算、动态仓位/杠杆、单笔最大可承受损失、交易频率、账户回撤/当日亏损/连续亏损、原社媒风险阻断/减仓以及原移动退出继续由旧模块执行。没有新增入场概率阈值。RANGE要求已验证箱体和正向中点收益空间，程序不因父选择RANGE就接受无效边界。

forecast_target独立放在交易意图事实，订单止盈来自上述共用函数。`PaperPositionRiskManager`和既有会计/资金费/标记/退出计算由适配器复用；没有复制一套方向策略。

### 5.2 持仓与结构风险

CLOSE绑定position_id+position_version，发送前再检查；已平仓或身份/版本变了则旧意图失效。父转向只形成平仓，不能同次平掉后自动反手。下一份新综合才可开反向仓位。

预测到期保留仓位，继续原止损/止盈/移动退出。持仓body另存structure_prediction_id、structure_verified_until及首次结构破坏记忆，按连续分钟回放原失效条件；停机期间曾失效、最新价格又回到边界内也不会漏掉。新同向综合可以接管结构判断，不能回写原报告。

子周期反向只排父综合。没有持仓不生成平仓意图。同向新报告不直接追加仓位。结构失效平仓cause包括预测/持仓ID和revision，部分成交导致版本变化时可生成针对剩余持仓的新意图。

### 5.3 准备、提交与未知结果

`ExecutionService`只在execution_enabled=true时加载执行设置；否则意图标DISABLED，不创建账户、不调transport。以后启用不会补发这些历史意图。

执行前读取旧app/risk/strategy/runtime配置，缺文件不以宽松默认替代；配置mode改变需重启校验。每次准备/监控重读运行与风险/策略值，发送前对比准备参数，防止旧准备结果绕过新暂停/参数。

开仓还要求父/子有效、当前head一致、分钟数据完整、验证进度跟上、报价默认不超过90秒，以及无持仓/账户限制冲突。市场/社媒/合约查询在事务外，保存或发送前重验任务、状态和参数。

```text
PENDING → PREPARED → SENDING → WORKING / PARTIAL / UNKNOWN
                                      → FILLED / CANCELED
校验或开关拒绝：REJECTED / STALE / DISABLED
```

客户端ID为 `pd + sha256(intent_id)前30位`，总32字符。PREPARED保存实际风险参数、市场、订单与记账进度；SENDING事务提交后才发送。此后即使重启，只有query资格，没有重新submit资格。

“已写SENDING但发送前崩溃”和“订单已接收但响应丢失”都可能查询不到；当前保守保留UNKNOWN等待核对。同账户未决订单阻止另一币种按相同权益重复定仓，吞吐下降是明确取舍。

### 5.4 成交、费用与撤余单

查询回报包含累计filled、平均价、费用和状态。数量不允许倒退/超出委托；相同累计数量的成交价格变更等冲突需核对。以累计差量计算新增成交价格、张数、费用，重复查询不会重复记账。

部分开仓合并数量和加权入场价；部分平仓按当前余仓比例计账并保留剩余；平仓用实际剩余张数，不以最新权益/价格重新估算。费用单独变化也同步到该持仓已实现盈亏；整仓平完才累计一次输赢，部分平仓撤单不提前累计连续亏损。

开仓尚未完全成交而预测结束/出现绑定平仓要求时，先查询确认pending，再撤同一订单未成交余量。撤单响应未知仍查询原订单。单纯预测到期只能阻止继续加仓，不关闭已经成交仓位。

### 5.5 交易所传输与自主退出

PaperTransport从冻结订单产生确定结果，可重启重现，不额外模拟随机滑点/费用。OKXTransport复用旧客户端、合约数量与风险参数；开仓附带固定SL/TP，平仓reduceOnly按实际张数。instrument与订单scope、有限正数量/价格、费用币种需校验。

初始附带保护价基于提交时行情，软件保护按实际成交价继续计算；两者在滑点下的差异及保护单接受情况须在线验收，不能把本地参数相等当成实盘保护必然有效。

交易所保护单自主平仓时，runtime通过只读权益/持仓/历史查询对账。要求唯一position ID或严格时窗/方向/数量证据；只使用真实历史realizedPnl，并扣除之前已入账部分。没查到持仓不等于可以用标记价生成退出事实。

账本外持仓、对冲模式、身份/数量冲突、无法换算的非USDT费用及非有限数值等会拒绝继续正常提交，保留状态供核对；本次没有自动认领人工调仓功能。

### 5.6 持久硬风控与版本切换

当日亏损、回撤、连续亏损或健康检查失败形成trading_accounts.body.risk_halt；跨币种和重启仍阻止开仓，平仓仍可执行。只有operator经既有POST `/v1/schedule/resume`解除，沿用幂等和审计；恢复后仍检查实际风险，未达标会再次拒绝。

首次加载旧账户已有持仓，或engine升级时尚有仓/未决订单，会拒绝自动切换，避免原结构和账本丢失。同账户旧CLI与新runtime禁止同时执行；线上切换由原入口处理完成再进行。未知状态不能通过换engine或删记录绕过。

## 六、自主配套与验证边界

### 6.1 用户已确认的内容与开发决定

多周期趋势/结构、父自主综合、原风控、失效/转向平仓、到期持仓、BTC/ETH、知识点题库及预测学习是用户要求。4根初始期限、0.25ATR、40回放/20影子、候选根数、0.02门槛等已在批准计划中写明，不应重新报告成开发者额外发明的业务功能。

以下17组包含未逐条规定的实现参数和必要配套；部分能力本身来自计划，表中记录的是具体实现决定与影响。

| 编号 | 具体决定 | 对运行的影响 |
|---|---|---|
| 1 | structural/legacy模式、默认执行关闭、account_key/legacy_config_dir、180秒输入/90秒报价 | 兼容旧历史、明确账户及配置来源，过旧证据不新开仓 |
| 2 | 结构hash/version、schema3与API v2 | 避免新事件概率与旧小时标签混淆 |
| 3 | 完整分钟、1～5分钟确认、多个候选及UNKNOWN回退 | 只使用可验证事件，不猜分钟内顺序 |
| 4 | 币种锁/账户锁、代际和语义hash、排队合并、过时重排、单模型/四确定性槽 | 控制异步竞态与重复工作 |
| 5 | 中文双字FTS5、最多500 PG候选、6000/12000正文、冻结recall | 检索资源有界；索引故障可降级但不是语义向量搜索 |
| 6 | 4份UNVERIFIED seed、复盘90天/200条、学习扫描200份 | 给初始检查步骤并控制上下文，样本不足不假装有经验 |
| 7 | 等待成本0.01/根、父/子1h同组期限、排除无法评分配对但保留记录 | 防止无限延长期限、父子重复计样本 |
| 8 | 抽取原风险函数、RANGE几何校验、保留社媒风险 | 保留原风险差异；社媒不代替父方向决策 |
| 9 | 每次执行重读风险/运行/策略并检查准备参数 | 准备后配置改变不会被旧订单绕过 |
| 10 | 持久订单状态、稳定ID、累计差量记账、未知状态账户占用 | 重启不重复发送；可能等待人工核对 |
| 11 | 撤未成交开仓余量、剩余张数平仓、持仓独立结构游标 | 过期不再加仓，已经持有部分继续保护 |
| 12 | 开仓附带保护、实际成交价软件退出、唯一历史退出对账 | 不把交易所自主平仓漏成永远持仓或虚构盈亏 |
| 13 | 已有仓/未决单拒绝自动切换engine/账户 | 升级需先由原版本处理完整，不自动接管外部调仓 |
| 14 | 新CLI/主控只读查询，旧迭代工具退役 | 新事实可查，模型没有额外交易/改代码权限 |
| 15 | PG风险暂停、operator恢复、整仓统计输赢 | 重启/换币种不能绕过账户限制，部分平仓不多计亏损 |
| 16 | Schema桥接白名单、部署hash一致、复盘提交恢复、v1/v2地址/异常数据校验 | 使部署、授权和崩溃后的正式事实一致 |
| 17 | 同源测试时钟、fixture包、58917本地PG、wheel检查、基线hash/Feature记录 | 属于开发验证配套，未改变线上状态 |

没有新增收益优化器、模型权重训练、向量数据库、自动部署、自动认领外部持仓或任意策略源码修改Agent。

### 6.2 验证范围和使用限制

既有交付记录：旧框架207项、新框架235项全量测试通过；新订单/权限/沙箱补验16项、最终控制地址改动原生插件定向1项通过。真实PG应用0009与不可变触发器；Hermes固定参考源码参与插件发现和原生委派，模型/订单响应为fixture。Ruff、uv依赖锁、三个wheel及资源导入检查通过。

2026-10-11补齐复盘320根输入后，新框架重新全量回归244项通过（无跳过）；新增9项覆盖BTC/ETH的预测120根与复盘320根、来源修订/重试仍复用冻结输入、7种缺失或非法窗口整批拒绝。单runtime测试实际等待两币种复盘完成，并检查沙箱收到四组80根。Ruff全目录及改动文件格式检查通过。原框架本轮未改未重跑，其207项仍为前次记录；真实模型对扩展复盘窗口的效果和耗时仍待验收。

本次同步规格为文档改动，不重复运行上述业务测试或把既有结果写成新增验收。检查正文/字段/源码路径/链接、历史副本hash和业务代码不变。完整证据见[验证与限制](../../../docs/prediction-driven-validation.md)。

仍需真实环境验收：模型结构判断与综合质量、预测统计/收益、Cube真实权限隔离、长期行情缺口/限流、实际合约精度/费用/保护单/部分成交、账户切换和备份恢复。UNKNOWN无限等待、账户串行造成延迟、外部调仓需核对、分钟内顺序不可知及几何参数的局限均保留。

交付时必须在对话中直接说明功能实现和自主配套，再链接完整细节；不能只给四个链接让用户自行查找其要求的第二、第三点。
