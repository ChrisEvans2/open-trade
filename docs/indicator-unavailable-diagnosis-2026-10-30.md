> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 指标无有效数值的根因核查

日期：2026-10-30（Asia/Shanghai）。范围：用户截图所指的2026-10-29最终C轮10份冻结输入，以及当前工作区源码。本次完成原因定位，没有实施业务修复。

26项基础指标在这10份输入中全部为OK。缺项集中在11项扩展指标：存在未完成的采集配置、正式预测接入遗漏、时间过滤造成的缺失，以及ETH指标适用性错误。不能统一解释成公式计算失败，也不能统一解释成外部接口故障。

## 逐项归因

表内数量是10份输入中的字段槽位数，不是独立指标数量或交易样本数。

| 指标 | 本轮状态及槽位 | 已核实的直接原因 | 性质 |
| --- | --- | --- | --- |
| `cvd`、`taker_buy_sell_ratio`、`oi_change`、`long_short_ratio` | 40个MISSING | 三类统计的时间戳语义配置均为null，采集器不请求相应统计端点，保存`UNVERIFIED_TIMESTAMP_CONTRACT`占位观测 | 配置/数据契约尚未完成 |
| `market_breadth` | 10个MISSING | 验收配置的`market_basket: []`，采集函数直接返回空结果 | 缺少明确的市场篮子配置 |
| `liquidation_imbalance` | 10个MISSING | 原始强平采样已落库，正式预测未调用窗口聚合，也未把结果送入冻结输入 | 正式流程接入遗漏，另有采样范围和历史覆盖限制 |
| `funding_rate` | 6个MISSING；1h子/父的4个为OK | 4h/6h/12h按各自最后收盘时间过滤，刚获取的已发布费率晚于这些边界，被排除；本次输入没有更早的已发布费率快照可替用 | 既有时间规则与输入供给造成的缺失 |
| ETH的`btc_correlation`、`btc_beta`、`relative_strength_vs_btc` | 15个错误的NOT_APPLICABLE | 三项无条件写入“BTC对自身不提供独立信息”，没有ETH计算分支和对齐基准输入 | 明确的实现错误 |
| BTC的上述三项 | 15个NOT_APPLICABLE | BTC与自身比较按现有设计不提供独立信息 | 正常不适用 |

40 + 10 + 10 + 6 = **66个MISSING**；ETH误标15个 + BTC正常不适用15个 = **30个NOT_APPLICABLE**。与原记录274个OK、66个MISSING、30个NOT_APPLICABLE完全一致。资金费标准分`funding_zscore`在10份输入中均为OK，不属于此次缺失项。

### 统计数据没有发起采集

验收配置的`taker_timestamp`、`oi_history_timestamp`、`long_short_timestamp`均为null，当前仓库配置也保留这三个空值。采集器在配置为空时直接生成MISSING观测；只有指定时点契约后才发起HTTP请求。

每份冻结输入有114条外部观测引用，其中12条是三类统计×四周期的未核验占位。10份输入中共出现120次`UNVERIFIED_TIMESTAMP_CONTRACT`，与离线备份一致。这能明确解释上述四项缺失，不能归咎于这轮网络超时。

普通OI当前值确实采到了，但`oi_change`要求相邻两根周期边界上的两个合约持仓量。备份中的当前值时间约为北京时间15:23—15:24，不能代替边界对齐的历史值。

证据：[统计采集分支](../trading-agent/src/trading_agent/market/observations.py#L166)、[OI配对条件](../packages/trading-quant/src/trading_quant/features.py#L257)、[当前配置](../trading-agent/config.yaml#L12)。

### 强平连接修复没有完成预测接入

`Runtime`创建并启动采样器，但构造`PredictionMarket`时没有传入采样器。预测准备阶段调用HTTP外部观测采集后就冻结输入，没有读取强平事实并生成窗口观测。

全仓当前有效Python代码中，`.windows(...)`的调用仅见于强平集成测试。量化计算需要的是`coverage="public_sample"`且覆盖到本周期收盘的窗口观测，不能直接消费`public_sample_events`原始事件。

备份中强平记录为OPEN 2、COVERAGE 20、EVENT 2、GAP 6，**WINDOW为0**；10份冻结输入的强平观测引用也均为0。由此确认不是仅有“连接正常但样本少”，而是预测的数据链路缺了一步。

另有两项独立限制：当前采样器只按配置的BTC合约保留事件，没有ETH覆盖；这轮两条原始事件发生于北京时间15:25:45.802和15:27:12.361，均晚于10份预测的冻结时间，不能用于补算原输入。即使接入修复，也不能保证这轮历史输入立即有数值，更不能回填已发布事实。

证据：[采样器初始化](../trading-agent/src/trading_agent/runtime/lifecycle.py#L92)、[预测对象构造](../trading-agent/src/trading_agent/runtime/lifecycle.py#L153)、[预测准备入口](../trading-agent/src/trading_agent/prediction/market.py#L132)、[聚合方法](../trading-agent/src/trading_agent/market/liquidations.py#L321)、[直接调用聚合的测试](../trading-agent/tests/integration/test_liquidation_sampler.py#L98)。

### 大周期资金费率被时间规则排除

4h、6h、12h使用最后收盘时间作为外部观测事件的截止点；1h允许使用决策时点前已发布的观测。C轮对应的北京时间收盘边界分别为12:00、14:00、08:00，获取的已发布资金费率事件时间约15:22—15:23，所以六份大周期输入都被过滤；四份1h子/父输入则保留它。

这是现有单元测试明确覆盖的时间语义。`funding-rate-history`提供的已结算费率可用于`funding_zscore`，却不是`funding_rate`读取的`published_rate`，不能直接互换。若希望大周期也有该项，应保留并按时点选取更早的已发布费率快照，或先明确当前背景费率与收盘时特征的不同语义；不能直接放宽时间校验。

证据：[时间过滤](../packages/trading-quant/src/trading_quant/features.py#L45)、[费率选择](../packages/trading-quant/src/trading_quant/features.py#L273)、[既有时间语义测试](../trading-agent/tests/unit/test_observations.py#L17)。

### ETH三项没有真正实现

共享量化函数先把扩展指标设为缺失，再无条件将三个BTC相对指标设为NOT_APPLICABLE，没有检查当前合约。其现有观测过滤还只接收当前合约的数据；正式预测仅采集当前合约80根K线，没有为ETH提供对齐的BTC基准。

所以修正不能只改一段中文理由：需要补齐ETH基准输入、对齐与公式计算，并区分基准缺失、样本不足及零方差等状态。BTC可以继续按现有设计标不适用；ETH缺少基准应标缺失，具备有效基准时才计算。

证据：[输入接口与同合约过滤](../packages/trading-quant/src/trading_quant/features.py#L32)、[无条件不适用分支](../packages/trading-quant/src/trading_quant/features.py#L182)。

## 为什么已有测试通过

现有基线主要用BTC的趋势/平价样例；BTC标不适用本来就符合现有设计，不能覆盖ETH错误。统计端点成功用例主动提供了时间配置，另一个用例明确验证未配置时不发请求。强平测试直接调用`sampler.windows()`再送入量化函数，绕过了正式预测的缺失接入。必需特征门禁只检查`return_1`、`return_20`、`atr_pct`、`rsi`，因此这些扩展缺项不会阻止发布。

测试通过能够证明已覆盖分支的行为，不能证明37项全部有效，也不能证明正式流程已传入所有数据。

## 本轮验证与限制

1. 尝试只读连接原隔离验收数据库未成功，改用已有`evidence.dump`离线检查；未启动旧验收集群或任何业务服务。
2. 用`pg_restore --data-only --table=... --file=-`读取三张事实表，未向数据库执行恢复或写入。选取与原核验文件相同的10份输入，核对全部输入hash，按各自引用的80根K线和外部观测重算。
3. **370项完整输出与冻结记录逐项一致**，包含数值、质量、原因、来源及引用。这是使用现有实现的事实重放，不是全部公式的独立数学正确性验证。
4. 当前重新运行以下现有单元集合：**41 passed in 7.77s**。

```bash
cd /srv/open-trade
trading-agent/.venv/bin/python .local/private-evidence

cd /srv/open-trade/trading-agent
.venv/bin/python -m pytest -q tests/unit/test_market.py tests/unit/test_quant_owner.py tests/unit/test_observations.py tests/unit/test_statistics.py tests/unit/test_breadth.py
```

离线脚本和脱敏结果位于忽略目录`.local/private-evidence`。没有业务实现变更，本轮未新增或运行真实PostgreSQL集成测试，也没有线上验收、模型调用或交易执行。

## 后续处理顺序与本次自主决定

建议先补齐ETH基准计算和强平预测接入，并增加覆盖正式冻结路径的验收；随后核验三类统计的实际时间语义，补完整周期窗口，明确市场篮子；大周期资金费率沿既有时间规则补充历史快照。此处是建议，均未实施。市场篮子成员和统计时间语义不能靠猜测补值，公开强平采样也不能冒充全市场总量。

本次自主选择离线检查已有备份，以避免恢复旧验收运行环境；只新增本报告、忽略的诊断材料并追加`Feature.md`工作记录。原有未提交代码和报告保持；不改配置、凭据、风险限制、预测/评分事实或运行状态，不提交、不推送，不改`changelog.md`。撤回本次新增报告及对应工作记录即可撤回文档交付，无业务状态需要回滚；已有验收备份与历史证据保留。

## 后续追问：公开API能否实际返回数据

用户继续追问，是API无法提供数据，还是请求后返回了空值。2026-10-30北京时间11:38:36—11:39:00，直接使用未签名、无凭据的公开HTTPS GET完成实测，没有经过生产采集器或修改其配置。

覆盖BTC-USDT-SWAP、ETH-USDT-SWAP，1h/4h/6h/12h四周期，以及主动成交、历史OI、合约多空账户比三个端点。每组分别请求最新100条和按当前采集器构造的时间范围取数（主动成交20周期、其余2周期，沿用边界前后1毫秒参数），共2×4×3×2＝48次请求。

| 公开端点 | 最新数据请求 | 按当前代码时间范围请求 | 返回状态 |
| --- | --- | --- | --- |
| `/api/v5/rubik/stat/taker-volume-contract` | 每组100条 | 每组21条 | 16次均HTTP 200、业务code=0 |
| `/api/v5/rubik/stat/contracts/open-interest-history` | 每组100条 | 每组3条 | 16次均HTTP 200、业务code=0 |
| `/api/v5/rubik/stat/contracts/long-short-account-ratio-contract` | 每组100条 | 每组3条 | 16次均HTTP 200、业务code=0 |

**48次均成功且非空，空数组0次，失败0次。** 这补充证明了本次检查时API确实能够提供所需类别的数据。原C轮的40个MISSING仍由时间配置为null、采集器提前跳过请求直接造成，不是已观察到的API空响应。

这项实测没有核准接口时间戳代表区间开始、结束或瞬时值，也没有把返回的21/3条原样视作合格的20周期窗口或边界配对；仍须完成时间解释、筛选、覆盖和未来数据隔离验证。当前成功不能倒推原验收时每次网络请求必然成功。本次没有修改配置或让预测流程重新运行，也没有回填原冻结事实。

复核命令：

```bash
cd /srv/open-trade
trading-agent/.venv/bin/python .local/private-evidence
```

忽略目录中的`api_probe.py`保存公开GET检查脚本，`api-probe.json`保存请求参数、状态、条数与时间摘要；没有读取凭据或保存私有交易所响应。[OKX官方统计接口文档](https://www.okx.com/docs-v5/en/#trading-statistics-rest-api)提供端点与字段说明。本轮仅补充只读核验、报告与工作记录，无业务代码变更，无需新增运行回归；未重新执行上一节的41项单元或PG集成。回退只撤本节和对应工作记录，已有验收事实及其他改动保持。

## 后续解释：其余指标的意义及API边界

用户要求继续解释市场宽度、强平不平衡、资金费率以及BTC相对指标，并区分接口能力、采集遗漏与其他原因。以下解释以当前代码和原C轮事实为依据，三个ETH相对指标的概念解释不代表其已实现。

- **市场宽度`market_breadth`**：当前定义为指定篮子中，最新收盘价高于本周期20根简单移动均线的有效成员占比。例如10个成员全部有合格数据，其中7个站上均线，结果为70%。它描述选定范围内的行情参与面。分母是有合格数据的成员数，部分成员缺失时另报覆盖质量。所需原料是各成员K线，由程序计算；目前篮子为空使采集函数提前返回，未请求成员数据。
- **强平不平衡`liquidation_imbalance`**：当前公式为“空头被强平金额减多头被强平金额，再除以两者合计”，衡量指定周期内强平金额偏向哪侧。正数表示空头被强平金额更多，负数表示多头更多，采用金额而非合约张数直接比较。已有BTC原始事件证明公开频道能够提供数据；正式预测缺少窗口聚合接入，ETH未纳入采样，原窗口也缺乏历史覆盖。官方明确公开消息不代表OKX全部强平；接入修复后仍应保留公开采样的PARTIAL_COVERAGE属性，不能宣称全市场完整数据。
- **资金费率`funding_rate`**：永续合约多空双方周期性交换资金所用的费率；正值对应多方向空方支付，负值反向。当前字段读取的是下一次结算的预计费率，结算前可能变化。原C轮实际已取得快照；大周期截止时间早于快照时点，所以被过滤。当前费率与历史已结算费率接口均能提供数据，但结算历史不等于每个历史时点的预估费率快照；程序需要保留并按时点读取快照，或明确变更特征语义，才能合规补齐。官方`ts`称数据返回时间，不能把它当作`fundingTime`结算时间。
- **BTC相关性`btc_correlation`**：通常比较ETH与BTC在相同时间窗口内收益率的线性相关性，范围为-1至1，分别描述反向、低线性关联与同向程度。通常使用收益率序列，不能仅凭名称宣称当前项目已有这一公式。
- **BTC贝塔`btc_beta`**：通常以ETH收益率与BTC收益率的协方差除以BTC收益率方差，刻画历史样本中ETH对BTC变动的敏感程度。例如1.5表示历史拟合的敏感度，不能理解为未来每次BTC涨1%、ETH就一定涨1.5%。
- **相对BTC强弱`relative_strength_vs_btc`**：描述同一段时间ETH表现相对BTC领先或落后。例如ETH涨5%、BTC涨2%，按收益率差口径领先3个百分点。当前尚未实现或明确该字段使用收益率差、累计收益比或价格比变化，示例不替代实现规格。

ETH三项相对指标通常需要对齐的两组K线，由程序计算。当前实现无ETH分支、无基准输入，无条件写“不适用”，属于实现遗漏和错误标记，并非API已经返回缺失值。BTC自身的三项则按现有设计省略：在非退化样本下自相关和自身贝塔为1，相对自身表现中性；NOT_APPLICABLE表示省略冗余信息。

参考：[OKX强平公开频道](https://www.okx.com/docs-v5/en/#public-data-websocket-liquidation-orders-channel)、[当前费率与字段](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate)、[历史已结算费率](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history)、[NIST相关系数定义](https://www.itl.nist.gov/div898/software/dataplot/refman2/ch2/correlat.pdf)、[NSE贝塔说明](https://www.nseindia.com/static/products-services/indices-beta)。

### K线与资金费原料补查

2026-10-30北京时间12:27:26—12:27:31，通过无凭据公开GET补查12次：BTC/ETH四周期各80根历史K线共8次，当前资金费率2次，历史资金费率2次。全部HTTP200/code0且非空；8组各80根均已收盘、连续并精确覆盖对应窗口；当前费率各1条且字段非空，历史各100条且已结算费率字段非空。

这证明本次检查时BTC/ETH的K线与费率原料可获取，支持把上述当前缺口归到篮子配置、跨币种计算接入和时间筛选；没有验证任意其他篮子成员、ETH强平覆盖或完整强平历史，也未新增实时强平订阅。强平能力依据已有实际落库记录与官方说明。

命令为`trading-agent/.venv/bin/python .local/private-evidence`，脚本与`market-funding-probe.json`在忽略目录保存。自主选择只补查必要公开原料，保留原48次统计API实测的独立口径，不混合计数。不修改业务代码、配置、凭据、运行状态、风险或历史事实；无业务变更，未重复单元/PG测试。回退只撤本节及对应工作记录，保留已有证据及其他工作。


后续：2026-10-31已开展外部指标补全，模块变化、剩余数据限制和独立审查见[本轮交付说明](indicator-completion-2026-10-31.md)。本报告保留原诊断时点，不回填历史事实。
