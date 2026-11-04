> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

> 2026-10-11更新：本文保留v1历史设计。当前结构预测/知识库以[新实现说明](../../../../../docs/prediction-driven-implementation.md)为准；旧策略代码迭代及memory-search/read活跃入口已退役，历史证据保留。

# 交易 Agent v1：技术细节与实施依据

> 状态：核心预测、评分、复盘与运维已有代码和本地验证；合约账本、收益实验及在线验收未完成。2026-10-08新增旧项目兼容入口与指标实现归属调整，详见 [当前实施状态](../../implementation-status.md)。
> 2026-10-10：策略迭代历史经验召回与 summary/content 分层已有本地实现，见第四部分；自动纠错、40% 重复率门槛和收益验证仍未实现。
> 主设计：[交易Agent-v1-实施设计.md](交易Agent-v1-实施设计.md)。本文件展开规则、实施建议和待定项。
> 本次已确认：OKX；每周期 80 根用于计算、最近 30 根给 Agent；每小时预测未来 1 小时；4h／6h／12h 提供趋势背景并复用。

## 先读这一页

| 技术主线 | 要解决的问题 | 状态 |
|---|---|---|
| [一、行情与验证](#一行情与验证) | 输入能否追溯，1h 预测怎样验证？ | 已展开 |
| [二、后端可靠性](#二后端可靠性) | 重复执行、超时、崩溃后怎样保持正确？ | 已整理任务、事务、恢复与验收 |
| [三、沙箱与权限](#三沙箱与权限) | Agent 可以执行什么、访问什么、提交什么？ | 已整理执行边界、授权与隔离验收 |
| [四、策略迭代经验记忆](#四策略迭代经验记忆) | 怎样分类召回历史改动，先看 summary，再展开 content？ | 已实现本地索引、冻结清单与引用；自动经验编辑与效果验证待做 |

当前流程：**冻结行情 → 复用或更新长周期趋势 → 1h 主预测 → 登记 → 1h 到期评分**。

策略迭代另走：**成交证据 → 搜索经验摘要 → 按需展开正文 → 候选修改 → 固定测试 → 记录结果与引用**。

渐进阅读：行情先看 1.1／1.3／1.6；后端先看 2.0；沙箱先看 3.1；经验先看 4.1。开发时再展开各节与验收清单。下文的指标参数属于实施建议，不能当成已经确认或验证的最优配置。

## 一、行情与验证

### 1.1 目标与责任边界

事前保存当时可用的输入和 1h 预测，事后用固定定价与评分规则验证。

| 主体 | 职责 |
|---|---|
| Python 程序 | 取数、校验、指标计算、快照、趋势缓存、发布校验、标签与评分 |
| 4h／6h／12h 子 Agent | 输出趋势状态、强度、结构和风险；每个新收盘边界更新一次 |
| 1h 子 Agent | 结合 1h 行情与三份趋势背景，输出未来 1 小时的三类概率 |
| 父 Agent | 整理解释与风险；正式概率取当前 1h 子报告 |
| PostgreSQL | 保存原始数据 revision、观测、快照、报告、预测与结算引用 |

长周期背景不再是与 1h 平均的三个独立概率，也没有等待 4h／6h／12h 结果的独立预测任务。节省 token 的依据是复用同一已收盘输入的趋势报告。

行情补采可用于指标初始化和计算验证；后来调用 LLM 的结果不能冒充历史正式预测。

### 1.2 模块与技术组合

以下是拟实现位置，不代表已有源码。

| 模块 | 工作 | 技术 |
|---|---|---|
| `market/` | OKX 行情与额外观测、周期对齐、指标与质量检查 | Python 3.11、httpx、asyncio、Decimal |
| `forecasts/` | 趋势缓存、1h 报告校验、发布、定价、标签与评分 | Pydantic 2、领域函数 |
| `memory/` | 固定快照、案例、统计和趋势报告引用 | context_bundle |
| `storage/` | 模型、repository、事务和不可变记录 | PostgreSQL 16、SQLAlchemy 2、psycopg 3、Alembic |
| `api/` | 结构化研究提交与查询 | FastAPI、Pydantic 2 |

模块均在 `src/trading_agent/` 下，同一个 runtime 装配。主要入口：

```text
build_snapshot(anchor)              # 80 根计算窗口 + 30 根模型视图 + 外部观测
ensure_trend_context(snapshot)      # 实施建议：查缓存，只更新到期背景
freeze_context(snapshot, engine)    # 固定案例与最终趋势引用
publish_forecast(attempt, payload)  # 登记当前 1h 概率和所有输入引用
settle_forecast(id)                 # 分阶段取得端点价，计算标签和评分
```

`ensure_trend_context` 是本次细化的建议接口；其他入口沿用主设计。任务 lease／fence 与进程恢复在第二部分展开。

### 1.3 时间契约

计算和存储使用 UTC；展示用北京时间；数据库使用 `timestamptz`，API 使用带 `Z` 的 ISO 8601。

每个整点为预测时段起点 T，每天 24 次。北京时间 16:00（UTC 08:00）的例子：

| 时间 | 行为 |
|---|---|
| 16:01 起 | 冻结行情；查趋势缓存；更新到期趋势，随后进行 1h 主分析 |
| 16:02 截止 | 无合格快照或尚未开始首个模型阶段，跳过本小时 |
| 16:04 截止 | 正式预测发布硬截止 |
| 16:05 | 预测窗口起点 |
| 16:06 起 | 起点 1m K 线收盘后，可取得最终起点定价 |
| 17:05 | 预测窗口终点 |
| 17:06 起 | 终点 1m K 线收盘后，可取得最终终点定价并评分 |

**恰好等于截止时刻允许，超过才拒绝**，最终数据库写入也检查截止时间。整批模型任务仍最多 120 秒，趋势与 1h 两阶段一起计时。

三个时间不能混淆：

- `anchor_at=T`：本小时起点，也是分析 K 线收盘边界上限。
- `available_at`：系统实际拿到某条原始观测并校验完成的时间。
- `decision_at`：本小时原始事实冻结、开始模型分析的时间。

原始输入必须 `available_at <= decision_at`；分析 K 线收盘边界必须 `<= T`。数据可以在 T 后的采集窗口获得，但不能包含 T 后才收盘的 K 线。

本 job 新生成的趋势报告属于冻结输入的派生结果，生成时间可以晚于 decision_at；本 job 重试时可复用先前 attempt 已完成的趋势阶段，但必须核对相同冻结输入。其他 job 的已有缓存报告须在 decision_at 前已经产生。最终 bundle 在 1h 主分析前冻结来源与引用，不能借此继续增加市场信息。

### 1.4 行情采集、数量与对齐

#### 两层输入数量

| 用途 | 每周期 | 四周期合计 | 12h 对应时间跨度 |
|---|---|---|---|
| 后台计算与完整证据 | 80 根已收盘 K 线 | 320 根 | 40 天 |
| 给该周期 Agent 的原始 K 线视图 | 上述 80 根中的最近 30 根 | 120 根 | 15 天 |

30 根是 80 根的子集，不能额外算成 110 根；也不能把“四周期合计 120 根”当成“每组必须 120 根”。长周期视图只在需要更新趋势时重新送给模型。

80 根是目前基础窗口。具体指标可能需要更多初始化或统计观测，必须检查 `required_history`。不足时标出原因；若扩展后台历史须记录新版本，30 根模型输入不随之自动扩大。

#### OKX 适配

逻辑标的 `BTCUSDT` 与 OKX `instId` 分开保存。真实合约类型待定，不能直接把 `BTC-USDT-SWAP` 示例当成已确认标的。

K 线使用公共 `/api/v5/market/candles` 或 `/api/v5/market/history-candles`。周期映射：`1h→1H`、`4h→4H`、`6h→6Hutc`、`12h→12Hutc`；使用 `confirm=1` 完整 K 线，按 ts 与周期长度构造内部半开区间。合约成交量字段区分张数、币数与计价币成交额，计算前统一单位。[OKX 行情文档](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks-history)

每组最近已收盘边界：

```text
expected_close_boundary = floor(T_epoch / interval_seconds) * interval_seconds
```

北京时间 16:00 时：

| 周期 | UTC 边界 | 北京时间边界 |
|---|---|---|
| 1h | 08:00 | 16:00 |
| 4h | 08:00 | 16:00 |
| 6h | 06:00 | 14:00 |
| 12h | 00:00 | 08:00 |

6h／12h 正常的收盘差异不能判成数据过期。严禁采用 OKX 默认 UTC+8 的 6H／12H 后仍套用 UTC 边界公式。

#### 数据质量与网络

每组 80 根计算数据、30 根模型视图分别校验数量和引用；检查有限正价格、有限非负成交量、`low <= open/close <= high`、无重复或缺口、连续时间步长、最新边界正确。服务器时钟偏差超过 5 秒或基础数据不合格，不发布本小时预测。

`httpx.AsyncClient` 连接超时 3 秒、读取超时 10 秒；网络错误和 5xx 最多重试 3 次，等待 1／2／4 秒；429 尊重 `Retry-After`，等待期间停止同域名请求。重试不能超过截止时间。不可达记录 `MARKET_UNAVAILABLE`，不自动换来源。

四周期可以并发采集；长周期未产生新 K 线时，可由程序复用数据库中的已校验版本，不需要再次调用模型。

### 1.5 指标集合、计算口径与额外数据

#### 指标参数的状态

**指标名称已经确认；下表的窗口和公式是实施建议，仍需锁定到 `feature_spec_version`。** 同名指标可能有多种口径，不能直接用任意库的默认参数替换。

记 C/H/L/V 为已收盘 K 线收盘价／最高价／最低价／统一单位成交量，r 为单根简单收益率。收益率默认以比例存储，显示时再转换百分数。

| 类别 | 指标 | 建议计算契约 | 数据依赖 |
|---|---|---|---|
| 价格 | Return 1/3/5/10/20 | `C_t/C_(t-k)-1`，k 为对应周期的根数 | close |
| 价格 | Log Return | 单根 `ln(C_t/C_(t-1))` | close |
| 趋势 | ADX | 建议 14；Wilder 平滑 DX，初始化单独锁定 | H/L/C |
| 趋势 | +DI/-DI | 建议 14；Wilder 平滑方向运动与 TR，输出 0–100 | H/L/C |
| 趋势 | Regression Slope | 建议最近 20 根 close 对索引做 OLS；斜率除以最新 close，单位比例／根 | close |
| 趋势 | Regression R² | 与上项同一回归窗口；常数序列标记 DEGENERATE，不造 R² | close |
| 趋势 | Efficiency Ratio | 建议 20；`abs(C_t-C_(t-20))/sum(abs(ΔC))` | close |
| 趋势 | Choppiness Index | 建议 14；`100*log10(sum(TR)/(HH-LL))/log10(14)` | H/L/C |
| 动量 | RSI | 建议 14、Wilder 平滑；纯上涨／纯下跌／全平初始化样例需锁定 | close |
| 动量 | ROC | 建议 20 根，复用 Return 20；明确比例或百分数单位 | close |
| 动量 | Momentum Acceleration | 建议 `r_t-r_(t-1)`，不混用价格二阶差分 | close |
| 波动 | ATR% | 建议 ATR14 / close；ATR 使用 Wilder，与旧简单平均版本区分 | H/L/C |
| 波动 | BB Width | 建议 20 根、2 倍总体标准差；`(upper-lower)/middle` | close |
| 波动 | Volatility Z-score | 建议单根收益的 10 根标准差，对之前 60 个同口径波动观测计算 Z | close，额外初始化历史 |
| 成交量 | RVOL | 当前 V / 之前 20 根 V 均值，基线不包含当前根 | 同单位 volume |
| 成交量 | Volume Z-score | 当前 V 对之前 20 根 V 的均值与总体标准差计算 Z | 同单位 volume |
| 成交量 | VWAP Deviation | 建议滚动 20 根的交易额／币数 VWAP，`close/VWAP-1`；字段单位须匹配 | quote turnover、base volume |
| 成交量 | CVD | 建议最近 20 根对应区间，累计主动买入币量减主动卖出币量 | 带方向成交或同口径买卖量 |
| 结构 | Distance to High | 建议前 20 根最高价 HH；`(HH-C)/HH`，突破时可为负 | high/close |
| 结构 | Distance to Low | 建议前 20 根最低价 LL；`(C-LL)/LL`，跌破时可为负 | low/close |
| 结构 | Breakout Strength | 建议向上突破 `(C-HH)/ATR`，向下突破 `(C-LL)/ATR`，区间内为 0；HH/LL 排除当前根 | H/L/C/ATR |
| 结构 | Close Position | `(C-L)/(H-L)`；零振幅标记 DEGENERATE | H/L/C |
| 合约 | OI Change | 建议相邻本周期边界的 OI 数量比值减 1，不混用 USD 估值与数量 | 对应合约 OI 时间序列 |
| 合约 | Funding Rate | 保存当时已公布的资金费率及对应生效时间；已结算与预测费率分字段 | 资金费观测 |
| 合约 | Funding Z-score | 建议最新已结算费率对之前 30 个已结算事件算 Z，不把一个费率复制成多条小时样本 | 历史资金费事件 |
| 合约 | Long/Short Ratio | 锁定账号比／仓位比、总体／大户、币种／合约范围；不混用 | 交易所统计 |
| 合约 | Taker Buy/Sell Ratio | 同一区间同单位主动买量／主动卖量；分母 0 时 null | 主动买卖量 |
| 合约 | Liquidation Imbalance | 建议 `(空头强平金额-多头强平金额)/(两者之和)`，正值表示空头强平占优 | 有覆盖标记的公开强平事件 |
| 大盘 | BTC Correlation | 建议同周期对齐的 60 根 log return 相关系数 | 其他标的与 BTC |
| 大盘 | BTC Beta | 同窗口 `cov(asset_return,BTC_return)/var(BTC_return)` | 其他标的与 BTC |
| 大盘 | Relative Strength vs BTC | 建议 20 根 `((C_t/C_(t-20))/(BTC_t/BTC_(t-20)))-1` | 其他标的与 BTC |
| 大盘 | Market Breadth | 建议固定篮子中 close 高于自身 SMA20 的比例；成分与覆盖率须先锁定 | 多资产行情篮子 |

Return／ROC 共用计算，避免当成两份独立证据。ATR 平滑算法本次属于待锁定的新口径；不能沿用旧实现后只改名称。Z-score 的标准差为零时输出 null 和原因，不能输出无穷大或填 0。

统计运算可使用固定版本数值库，需锁定 ddof、平滑初始化、零分母和舍入规则。金额、数量从原始字符串构造 Decimal；浮点统计不得反写金额账本。展示精度与完整内部值分开保存。

#### 额外数据实施规则

| 数据 | 采集与存储要求 |
|---|---|
| CVD／Taker | 持续采集带方向成交或可靠区间买卖量；记录区间覆盖与单位；不能由 K 线涨跌代替成交方向 |
| OI | 候选接口 `/api/v5/rubik/stat/contracts/open-interest-history` 与 `/api/v5/public/open-interest`；锁定合约与数量单位，按本周期边界选择观测，不对未知数据做未来插值 |
| Funding | 候选接口 `/api/v5/public/funding-rate` 与 `/api/v5/public/funding-rate-history`；公布信息与实际结算分字段，保存采集／生效时间；Z-score 使用实际结算事件 |
| 多空比 | 优先核对合约范围的 `/api/v5/rubik/stat/contracts/long-short-account-ratio-contract`，与币种级账号比区别；窗口语义仍需验收 |
| 爆仓 | 公开事件采集、去重、断连缺口记录；只报告所采集覆盖，不宣称全市场总量 |
| Market Breadth | 固定资产列表与市场类型；成员也使用当时可用的数据；记录覆盖数量和最低覆盖门槛 |

OKX 公开强平频道 `liquidation-orders` 明确不是全部强平量，所以由其计算的指标必须标记为公开采样口径；断连不能解释为没有爆仓。[OKX Public Data 文档](https://www.okx.com/docs-v5/en/#public-data-websocket-liquidation-orders-channel)

OI 与合约统计依赖公共数据／Trading Statistics 接口。合约主动买卖量可核对 `/api/v5/rubik/stat/taker-volume-contract`，同一单位的区间买量减卖量可形成 CVD；完整区间和时间语义必须确认。历史资金费的 `realizedRate` 与公布／预测费率区别保存，不默认固定 8 小时收取。具体端点权限、时间含义、粒度、分页和历史范围在适配器验收时锁定。[OKX Trading Statistics 文档](https://www.okx.com/docs-v5/en/#trading-statistics)、[资金费历史文档](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history)

当前仅分析 BTC：BTC 与自身的 Correlation、Beta、Relative Strength 不提供独立信息，记 `NOT_APPLICABLE`，不向模型当作有效证据。Market Breadth 可作为 BTC 背景，但篮子未锁定前标记未配置。

每个特征保存 `value/unit/window/source/event_at/available_at/quality/missing_reason`。质量区分 OK、MISSING、INSUFFICIENT_HISTORY、PARTIAL_COVERAGE、DEGENERATE、NOT_APPLICABLE。必需／可选特征集合写入配置并生成版本：必需项缺失跳过；可选项缺失可以明确说明后继续。不把 null 当 0，也不假装全部特征已经接通。

#### 不可变快照

快照保存四组 80 根计算数据的 revision、30 根模型视图、指标参数与质量、外部数据观测引用、每组最后边界、decision_at 和 hash。行情订正追加 revision，旧快照仍引用旧数据。

实施建议：使用固定序列化后计算 SHA-256，锁定 UTC 格式、Decimal 表示、字段范围与排序。趋势缓存的 input_content_hash 只覆盖该周期实际使用的固定输入，排除纯采集时间变化、其他周期数据和本小时重新检索的案例；仍包含真实数据 revision 与特征版本。不能直接使用整个四周期 snapshot hash，否则 1h 更新会使 12h 缓存每小时失效。

### 1.6 长周期趋势缓存与 1h 发布

#### 为什么缓存

长周期反映趋势背景，同一根已收盘 K 线没有改变时，重新分析没有新增输入。无需每小时重新调用 12h Agent，也无需等一个独立 12h 预测的结果。

| 周期 | 常规模型更新频率 | 角色 |
|---|---|---|
| 1h | 每小时 | 未来 1 小时的正式概率 |
| 4h | 每个新 4h 收盘边界 | 趋势背景 |
| 6h | 每个新 6h 收盘边界 | 趋势背景 |
| 12h | 每个新 12h 收盘边界 | 趋势背景 |

#### 缓存命中条件

```text
source + instId + timeframe + last_close_boundary
       + input_content_hash + engine_hash
```

报告不可变并持久化入 PostgreSQL，不能只放进程内存。缓存键不含本小时 job_id、attempt_id 或重新采集时间；输入修正、engine／特征版本改变会失效。

复用其他 job 的缓存需同时满足当前期望边界相等、版本一致、报告已在 decision_at 前产生、来源证据被本 bundle 授权。本 job 先前 attempt 已完成的趋势阶段也可复用，即使生成时间晚于原始 decision_at，仍需证明原始输入属于同一冻结快照及阶段。报告来自旧 attempt 是允许的，旧任务的令牌和沙箱不能随之复用。

自然有效期截止到下一根本周期 K 线收盘。北京时间 16:00 任务：4h 报告到 20:00，6h 的 14:00 报告到 20:00，12h 的 08:00 报告到 20:00。17:00～19:00 可复用；20:00 必须针对新收盘数据更新。

首次启动没有背景报告、输入订正和版本变化属于允许重建的特殊原因，日志记录 refresh_reason。新的小时资金费／OI 信息放入当前 1h 冻结输入，不应让尚未收盘的长周期反复刷新；长周期外部观测按本周期收盘边界选择，并满足实际可用时间限制，不能用新公布观测不断替换已冻结背景。

#### 执行依赖与失败处理

1. 冻结当前小时原始行情与额外观测，记录 decision_at。
2. 查询三份有效趋势；只并行补齐需更新的周期。
3. 校验背景报告，固定缓存或本次派生结果的来源，并冻结最终 bundle。
4. 运行 1h 子 Agent，输入最近 30 根 1h K 线、全部合格指标和三份趋势摘要。
5. 校验 1h 概率；父 Agent 整理解释；发布正式预测。

本次生成的趋势晚于 decision_at，但只能引用当时冻结的原始输入；这是模型派生过程，不是放宽取数时间。任一必需背景缺失／过期或 1h 失败，截止前重试，否则跳过，不用旧趋势冒充新趋势。

同一 attempt 重复调用返回已有结果。新 attempt 复用合法冻结证据，但当前 1h 概率必须来自本 attempt。刷新趋势的阶段与 1h 分析合计受 120 秒上限约束；需测量串行依赖带来的耗时。

#### 正式提交

入口 `POST /v1/research/forecast` 使用 schema_version=2：

```json
{
  "schema_version": 2,
  "one_hour_analysis_id": "当前attempt的1h报告ID",
  "trend_analysis_ids": {"4h": "报告ID", "6h": "报告ID", "12h": "报告ID"},
  "p_up": 0.6,
  "p_flat": 0.3,
  "p_down": 0.1,
  "reason": "解释当前1h判断与长周期背景的关系",
  "risk_notes": [],
  "evidence_ids": ["当前bundle授权的证据ID"]
}
```

三类概率有限、在 `[0,1]`、总和误差不超过 `0.000001`；提交值与 1h 报告逐项误差不超过同一阈值，保存校验后的 1h 原值，不做四周期平均或自动归一化。

1h 报告属于当前 attempt／bundle，趋势引用等于冻结清单并有效。拒绝未知字段和越权证据；理由最多 800 字；风险最多 3 条、每条 150 字。身份、symbol、时段、engine、strategy 和发布时间由服务端绑定。

预测标签取最大概率类别，完全相等时按 FLAT→UP→DOWN。`(symbol,anchor_at)` 唯一；同 payload hash 返回已有结果，不同内容返回 409；发布后不改写。

### 1.7 端点定价、标签与评分

窗口是 `[T+5分钟, T+1小时+5分钟]` 的两端价格比较。采用两端对应 1m K 线开盘价，K 线收盘并校验后才作为最终定价。先登记起点定价，到期后再拿终点；缺任一端点进入 AWAITING_DATA，不用最近价格替代。

标签阈值本次暂沿用旧定义，未证明适合 1h；后续人工修改须产生新的任务定义与评估版本。

```text
r = P_end / P_start - 1
UP   : r >  0.005
DOWN : r < -0.005
FLAT : -0.005 <= r <= 0.005
```

恰好 ±0.5% 都是 FLAT。程序计算：

```text
correct = predicted_label == actual_label
brier = Σ(p_k-y_k)²，k∈{UP,FLAT,DOWN}
```

实际上涨时 y=(1,0,0)，预测 (0.6,0.3,0.1) 的 Brier 为 `(0.6-1)²+0.3²+0.1²=0.26`。使用不除以类别数的版本，范围 `[0,2]`、越小越好；三类均等概率基线为 2/3，方向基线为 FLAT。

评分写入 settlements，原预测不变；行情订正追加 settlement revision 和 supersedes_id。报表显示样本量、类别分布、方向正确率、平均 Brier、固定基线与覆盖率。

```text
发布覆盖率 = 按时发布的正式1h预测数 / 计划小时数
```

失败和跳过仍计入分母。旧 4h 预测与新 1h 预测、不同标签／指标／engine 版本分别评估。趋势报告不算额外预测样本。评分也不代表模拟交易的费用后收益。

### 1.8 数据模型与约束

| 表 | 核心约束 |
|---|---|
| market_bars | source／instId／周期／开盘时间／revision 唯一；数据订正追加 |
| market_observations | 记录成交、OI、资金费、强平和市场篮子观测；包含单位、范围、可用时间和质量 |
| snapshots | 每周期 80 根计算数据、最近 30 根模型视图；外部引用与参数冻结 |
| context_bundles | 记录快照、历史案例与三份授权趋势报告引用；在 1h 分析前冻结 |
| trend_analyses | 4h／6h／12h；缓存键唯一；含来源快照、边界、版本、产生时间、有效边界、producer_job_id 与 producer_attempt_id |
| timeframe_analyses | 保存当前 attempt 的 1h 概率；同 attempt／周期唯一 |
| forecasts | symbol／anchor_at 唯一；引用当前 1h 报告与已授权趋势背景；正式事实只追加 |
| settlements | forecast／revision 唯一；修正关联旧版本，不覆盖 |

主键 UUID；金额与数量 numeric(28,12)，概率 numeric(12,9)，结构化 payload jsonb。外键保证引用关系；CHECK 约束状态。对预测、结算等事实表应用角色只授予 SELECT／INSERT；后台缓存查询通过领域服务授权，不由模型任意读取旧任务。

### 1.9 开发顺序与验收

| 顺序 | 工作 | 完成依据 |
|---|---|---|
| 1 | 锁定 OKX 合约与数据契约 | instId、周期、单位、时间和额外观测可核对 |
| 2 | 锁定特征窗口、公式与质量规则 | feature_spec_version；每项 required_history 和必需／可选定义 |
| 3 | 实现采集、校验、指标与快照 | 80／30 数量正确、手算一致、时间过滤生效 |
| 4 | 实现趋势报告持久缓存 | 只刷新到期周期，重启复用，来源与失效可追溯 |
| 5 | 实现两阶段委派与 1h 发布 | 新趋势齐备再主预测；原生子任务限额、硬截止与 schema 校验通过 |
| 6 | 实现端点定价、1h 评分与报表 | 无价格替换、无重复修订、覆盖率完整 |

最低验收清单：

- [ ] 四周期各 80 根用于计算、最近 30 根用于模型；30 是 80 的子集。
- [ ] OKX 6Hutc／12Hutc 边界正确；未完成 K 线不能进入快照。
- [ ] 所有指标有固定公式、单位、初始化与零分母行为；Return／ROC 不重复解释为独立证据。
- [ ] CVD 不由 K 线方向替代；Funding Z 用真实事件；缺采样不当成没有成交／爆仓。
- [ ] BTC 自身相对 BTC 三项指标为 NOT_APPLICABLE；篮子有版本与覆盖标记。
- [ ] 历史补采保留真实 available_at；后来的观测不能回填到过去。
- [ ] 同一根 12h 连续小时只分析一次；新收盘／订正／版本变化会刷新。
- [ ] 缓存不因 job_id、1h 行情更新或本小时案例变化失效；重启可复用；过期／未来／越权背景被拒绝。
- [ ] 本 job 重试可复用已完成的趋势阶段，不把合法派生报告误判成决策后的新市场信息。
- [ ] 本次新趋势允许晚于 decision_at 生成，但只能引用该时点前冻结的外部输入。
- [ ] 当前 1h 报告引用三份有效背景；正式概率与其一致，不四周期平均。
- [ ] 恰好等于截止允许；超过拒绝；完整两阶段模型任务不超过 120 秒。
- [ ] 16:05～17:05 的窗口按准确两端定价；任一端点缺失等待。
- [ ] ±0.5% 标签边界正确，Brier 手算示例为 0.26；旧 4h 成绩单独分组。
- [ ] 原预测不可改写；结算修订保留旧记录；失败／跳过计入覆盖率。

pytest 覆盖计算与边界；真实 PostgreSQL 验证引用、唯一键和不可变权限；真实固定版 Hermes 验证顺序与缓存授权。固定行情与模型输出仅用于 fixture，与真实结果分开。

在线连续 7 天记录 168 个计划小时及其状态，并记录各周期实际模型调用、缓存命中和耗时。这是运行验收，不证明收益或预测优势；小时样本也不能直接拿去混入原 4h 实验。

### 1.10 待锁定事项

| 事项 | 影响 |
|---|---|
| 合约类型、instId、结算资产 | 正确价格、成交量单位与合约统计 |
| 指标参数、平滑、初始化、ddof、舍入、必需／可选集合 | 80 根是否足够，缺失时是否跳过，结果能否复现 |
| CVD／OI／多空比／Taker 的数据覆盖与统计范围 | 是否真正具备该指标，能否跨周期汇总 |
| Funding 事件类型、公布／生效时间和历史数量 | 防止资金费未来信息或复制样本 |
| 强平公开采样与断连处理 | 不把样本称为全市场总量 |
| 市场篮子与最低覆盖率 | Market Breadth 的可解释性 |
| 缓存内容 hash、趋势 schema 与状态枚举 | 正确复用与失效，禁止模型自由改周期 |
| 基于新指标的案例状态分类与阈值 | 撤销旧 SMA／ATR 分类后，检索如何匹配 |
| 1h 标签阈值是否继续 ±0.5% | 本次暂沿用；若改，版本化后独立评估 |
| 两阶段模型实测耗时与部署行情连通性 | 能否在截止时间前完成 |

杠杆、仓位、费用、资金费记账和强平仍属于模拟合约后续规格；不能由预测的两个端点推导期间风险与完整账户收益。

### 1.11 本次对齐记录

2026-10-06：保留用户修改的 OKX 来源、80 根计算／30 根模型输入、截止时刻相等允许；修正每组不足 120 根的错误校验与旧 240 根说明。按新指标清单替换旧指标，增加外部观测依赖。正式预测按用户确认改为每小时预测未来 1 小时；长周期改为新收盘后更新、其他小时复用。同步修改主设计的调度、schema、配置、数据表与 A19～A21 验收。

主设计对应：第 3.4 节角色与缓存、第 4 节时间、第 5 节行情特征、第 6／7 节冻结证据与数据模型、第 9.1 节评分、第 14／15 节验收。

## 二、后端可靠性

### 2.0 不熟悉后端，也能怎样验收

**当前状态：设计已整理，官方机制资料已核对；应用尚未实现，故障测试尚未运行。** 本文件和原理出处不能代替真实实现的验证。

你主要确认业务应该怎样表现，实施者负责给出复现方式、实际结果与证据。下面先看可观察行为，再按需要读 2.1～2.9 的技术机制。

| 故障或操作 | 预期结果 | 实施者应交付的证据 |
|---|---|---|
| 同一小时同时触发 20 次 | 同一业务 job 与正式 forecast 各只有一条 | 独立数据库查询、返回 ID、并发测试报告 |
| 已保存预测后立刻杀掉 Agent 进程 | 原预测保留，恢复时不再次生成它 | forecast ID／hash 前后对比、模型调用记录 |
| 写入业务结果与通知记录之间故意抛错 | 同一事务的内容全部提交或全部回滚，没有半份结果 | 提交前／后故障注入、业务表与 outbox 查询 |
| 暂停旧任务，让新执行者接手，再恢复旧任务 | 旧任务提交被拒绝，不能改变新执行者的结果 | 两个独立执行者的时序、拒绝响应、数据库最终状态 |
| 让 Agent 一直不返回 | 到截止后失去提交资格；所属进程被有界终止；接口与心跳仍工作 | 令牌拒绝结果、进程退出状态、心跳时间线 |
| 消息渠道暂时无法发送 | 审批／日报等业务结果仍保存；通知可查为待重试或失败 | 业务 ID、outbox 状态、恢复后的发送记录 |
| 服务停机几个小时后重启 | 错过时段记录为跳过；已有预测继续验证，不生成历史假预测 | 时段清单、恢复前／后待结算数量及最终结果 |
| 关键后台循环停止，但 API 还活着 | 系统报告未就绪，并产生外部健康检查证据 | readyz 返回、循环心跳、doctor 记录 |

以上均是**待执行验收场景**，不是已经通过的测试。后续每项记录：场景、复现命令、预期值、实际值、PASS／FAIL、源码提交、依赖版本、报告与日志位置。不能仅贴一条「成功」日志作为证明。

验证顺序：先用真实 PostgreSQL 和可控行情／模型做并发与故障注入，再跑固定版 Hermes 接入测试，最后做在线连续运行。测试以业务不变量为断言，用独立数据库连接检查结果，避免只复制实现逻辑或只验证 mock 返回值。

修改相关代码后重跑对应验收；只有实际通过的场景可以标记已验证。原理核对属于设计审查，测试属于实现验证，在线报告属于运行证据，三者分别记录。

### 2.1 先看主体：四个保证

| 要保证什么 | 实施机制 |
|---|---|
| 同一业务重复请求或重试，不重复产生结果 | 请求幂等、业务唯一键、数据库事务 |
| 任务超时或换了执行者，旧结果不能继续写入 | lease、心跳、单调递增 fence、提交时校验 |
| 服务崩溃后，已完成阶段保留、未完成阶段继续 | PostgreSQL 持久任务和阶段、启动恢复、进程与沙箱归属核对 |
| 通知失败不影响预测、结算和审批 | 同事务写 outbox，事务外投递，独立重试 |

主流程：**调度登记任务 → 短事务领取 → 在事务外执行 → 短事务提交阶段／结果 → 恢复或完成**。

以下是待实现方案。具体阶段名属于实施建议；租约、并发、超时与恢复边界沿用主设计。

### 2.2 单一 runtime 与生命周期

`ta serve` 启动一个 Uvicorn worker，由 FastAPI lifespan 建立模块依赖、数据库连接池和后台循环。领域模块直接调用同进程对象，只有 Hermes 插件和人工 CLI 经本地 API 进入。

| 循环 | 频率 | 职责 |
|---|---|---|
| scheduler | 启动立即扫描，随后每 60 秒 | 创建预测、到期验证、日报和实验任务 |
| executor | 每 2 秒 | 领取持久任务，执行领域函数或启动 AgentRunner |
| notifier | 每 5 秒 | 领取 outbox，在事务外发送通知 |
| recovery | 每 15 秒 | 心跳、过期租约扫描、后台循环健康记录 |

确定性任务并发上限 4，顶层模型任务上限 1。`Semaphore(1)` 控制本进程的父 Agent；长周期趋势批次可以在该任务内部并行。I/O 与子进程等待使用异步接口，阻塞计算进入有界线程池，避免模型执行期间心跳停顿。

FORECAST 优先；每个整点前 5 分钟至后 4 分钟，不启动新的解释／复盘等低优先级模型任务。

启动顺序：配置和迁移校验 → 专用数据库连接取得单实例 advisory lock → 建立依赖 → 恢复扫描 → 启动循环 → readyz 成功。第二个实例拿不到锁立即退出。

单实例锁必须由存活的专用 PostgreSQL 会话持有，不能随普通连接池归还。失去持锁连接时，停止调度、终止在途 Agent 并退出，由进程管理器重启；不能在锁状态未知时继续执行。

关闭顺序：readyz 失败 → 停止领取新任务 → 最多等待 20 秒 → 终止残留 Agent 并撤销令牌 → 关闭连接池和释放实例锁。已提交事实保留，未完成工作由下一次启动恢复。

### 2.3 任务持久化与幂等

任务记录写入 PostgreSQL，内存队列只能用于唤醒，不能成为唯一事实来源。

| 类型 | dedupe_key | 特点 |
|---|---|---|
| FORECAST | `forecast:BTCUSDT:{T}` | 一个小时最多一个正式预测任务 |
| SETTLE | `settle:{forecast_id}:{pricing_revision}` | 起点定价和终点评分分阶段推进 |
| REVIEW_DAILY | `review:{UTC日期}` | 处理半开日区间，迟到结果追加报告修订 |
| EXPERIMENT | `experiment:{engine_hash}:{UTC周一日期}` | 避免重复创建同一计划实验 |
| EXPLAIN | `explain:{forecast_id}:{request_id}` | 按已保存事实生成解释 |

三个层次分别校验：

1. **请求层**：创建任务／导入／审批接口要求 Idempotency-Key。保存 actor、route、key、request_hash 与响应；同 key 同内容返回原响应，同 key 不同内容返回 409。
2. **任务层**：jobs.dedupe_key、schedule_slots(kind,scheduled_at) 使用唯一约束。重复 scheduler tick、重启扫描或请求不能再创建同一业务机会。
3. **事实层**：预测、结算 revision、成交、账本事件和通知事件各有业务唯一键。任务执行次数增加，也不能增加同一事实的落账次数。

唯一键冲突时，核对已存在记录的内容与归属，再返回已有结果；不能只捕获异常并报告成功。建议使用 `INSERT ... ON CONFLICT (...) DO NOTHING RETURNING ...` 避免预期冲突使事务进入失败状态；未插入时再按唯一键查询、核对。在采用 READ COMMITTED 时，用后续语句读取并发事务已经提交的记录；不能假定同一条语句的旧快照必然看到它。若使用异常路径，则须先回滚对应 savepoint 或事务再继续。事实表不能以 DO UPDATE 覆盖旧预测。幂等请求记录与实际写操作在同一事务提交。[PostgreSQL INSERT](https://www.postgresql.org/docs/16/sql-insert.html)

### 2.4 lease 与 fence：阻止旧执行者写结果

lease 表示任务在一段时间内归谁执行；fence 是每次重新领取时增加的执行代号。租约过期后旧进程可能仍在运行，因此仅靠超时或终止进程不足以保护数据库。

领取使用短事务：

```text
SELECT 一个到期且可领取的 job FOR UPDATE SKIP LOCKED
设置 RUNNING、lease_owner、lease_until = 数据库当前时间 + 60秒
fence 增加 1，创建关联此 fence 的 job_attempt
提交事务后，才调用外部行情或模型
```

每 15 秒续租。续租必须匹配 job_id、attempt_id、lease_owner、fence 与允许状态，且旧 lease 尚未过期；影响行数为 0 表示执行资格已失效，应停止执行并使当前令牌不可用。即使尚无人重新领取，已经过期的租约也不能通过迟到心跳复活；要继续执行必须重新领取并增加 fence。

所有阶段和结果提交在同一个短事务中锁定 job 行并检查：

- 当前 attempt／owner／fence 与领取时一致，任务仍允许写入，lease 未过期。
- 任务令牌未撤销，提交对象属于本任务。
- FORECAST 尚在发布截止内，输入和报告版本匹配。
- 业务唯一键未出现不同内容的既有事实。

检查与 INSERT／阶段推进必须在同一事务，不能在 HTTP 请求开始时检查一次、模型跑完后直接写。

租约和发布截止按数据库实际时钟判断，先取得所需行锁，再临近写入读取 `clock_timestamp()`；不能用等待行锁前的应用时间，也不能把 `now()`／CURRENT_TIMESTAMP 当成事务内一直变化的当前时间。它们在 PostgreSQL 中表示事务开始时间，可能放行已经超过截止的结果。published_at 的定义为最终校验和写入时刻，COMMIT 可稍后完成；事务时间仍需有界。[PostgreSQL 时间函数](https://www.postgresql.org/docs/16/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)

例如 A 领取时 fence=7，失去心跳后 B 重新领取 fence=8。A 即使后来返回合法 JSON，也不能更新阶段、保存新趋势、发布预测或续租。已被合法提交的不可变报告仍可按授权规则读取。

同一账户开仓与结算按事件时间串行，使用账户行锁；跨账户操作按 account_id 固定顺序加锁，减少死锁。进程内 Semaphore 与数据库 fence 的职责不同，两者都要落实。

### 2.5 阶段记录与事务边界

实施建议采用下列 FORECAST 阶段：

```text
PLANNED → SNAPSHOT_FROZEN → TRENDS_READY
        → CONTEXT_FROZEN → ONE_HOUR_ANALYZED → PUBLISHED
```

快照提交后不重采输入；已经合法完成的趋势阶段可复用；最终 bundle 提交后不重新检索或替换背景。当前 attempt 的 1h 报告独立保存；新 attempt 的 1h 概率不能混用旧 attempt 报告。

模型执行超时和格式修复不产生新的市场输入。首次模型阶段开始时间需持久记录，恢复时不能伪造开始时间以绕过 T+2 分钟的门槛。

SETTLE 阶段建议：

```text
WAIT_ENTRY_PRICE → ENTRY_PRICED → WAIT_EXIT_PRICE → SETTLED
```

等待未来端点时保存 next_run_at 并释放执行租约；端点已经到期但行情缺失时进入 AWAITING_DATA。只有两个最终定价都齐备才写 settlement。预测超过截止可以跳过，已有预测的验证不能因停机或过期而放弃。

| 提交动作 | 同事务保存 |
|---|---|
| 发布预测 | forecast、启用模拟时的预定决策、job 阶段、audit |
| 结算 | settlement、当前可计算的模拟成交／账本、实验推进、outbox、job 状态 |
| 策略审核 | approval、计划 activation、audit、outbox |
| 请求幂等 | 对应写操作与原响应记录 |

不把数据库事务跨越行情 HTTP、LLM 调用或消息发送。先登记意图，事务外执行，再以有效 fence 提交；数据库结果是成功依据。

### 2.6 AgentRunner 超时与成功判断

使用 argv、shell=False 和异步子进程接口启动独立 trading Profile。记录 PID、进程启动时间、Hermes 会话、job／attempt 与 sandbox_ref，避免 PID 重用导致误杀。

父任务内的趋势刷新、1h 分析、汇总首轮共 90 秒；格式修复计入 120 秒总墙钟时限，同时受 T+4 分钟发布硬截止限制。实际截止取两者更早者。到期终止整个进程组、撤销令牌并清理本 attempt 的沙箱；终止等待也要有界。

`asyncio.wait_for()` 的超时不能当成已杀掉子进程。实施时显式建立受管理进程组；超时或取消后使令牌失效，对所属进程组发送终止信号，等待配置的宽限期，仍存活则强制终止并回收进程。使用 stdout／stderr PIPE 时持续读取，或用 communicate 排空，避免只 wait 导致管道写满而卡住；输出需有长度与脱敏限制。远程沙箱内的进程还需通过 provider 清理，并核对清理结果，宿主机进程组退出不等于远程资源已销毁。[Python 异步子进程](https://docs.python.org/3.11/library/asyncio-subprocess.html)

子进程退出码 0 或自然语言 stdout 都不能证明预测发布。AgentRunner 必须查询数据库中的正式 forecast 和归属：

- 已存在合法 forecast：发布成功；CLI 后续崩溃不重跑模型。
- 没有 forecast：按结构错误、传输失败或超时处理；不能宣布完成。
- fence 已失效：旧 attempt 不能继续提交，即使进程尚未退出。

模型不可用时，在总截止内最多 2 个 attempt，不自动切模型。格式修复最多一次；每次都遵守冻结上下文、原生子任务限额与总时限。

### 2.7 重启恢复与失败策略

启动取得单实例锁后，先核对旧进程 PID 和启动时间；仅回收明确属于本项目的旧进程／沙箱，撤销旧令牌。不能仅凭 PID 或沙箱名称删除其他任务资源。

随后按持久事实恢复：

| 发现的状态 | 处理 |
|---|---|
| 已有正式 forecast，但 job 未完成 | 补齐任务完成状态，不调用模型重做 |
| 已冻结输入且未过发布截止 | 租约过期后重新领取，按已提交阶段恢复；沿用快照和证据 |
| 正式预测时段已经错过 | 记录 SKIPPED 与原因，不补造历史预测 |
| 已有预测未结算 | 按保存端点和阶段继续，逐条补结算 |
| 数据库不可用 | 不发布、不报告就绪；可恢复后继续未完成结算 |
| 模型／必需趋势失败 | 截止内受限重试；失败原因保留，不以过期趋势替代 |
| 暂缺结算行情 | 按 1／5／15／60 分钟退避，之后每小时重试；持续 24 小时提醒 |

任务状态沿用 QUEUED、RUNNING、RETRY_WAIT、AWAITING_DATA、SUCCEEDED、FAILED、SKIPPED。阶段表示业务走到哪里，状态表示现在能否执行，二者分别存储。

### 2.8 通知 outbox

业务事务先写 outbox；notifier 用自己的领取事务获取事件，随后在事务外调用 `hermes send`。发送完成后用当前 claim_id 确认，旧投递 attempt 不能确认新领取的事件。

每轮最多领取 20 条、发送并发 4、claim 租约 120 秒、单条发送超时 10 秒。只对可识别成功结构记 SENT；明确失败按 1／5／15 分钟退避、最多 5 次。完整退避序列和 UNKNOWN 的重试规则需在实施前锁定。

消息可能已经发出，但进程在记录成功前崩溃，此时是 UNKNOWN。重试可能重复投递，所以外部消息不承诺恰好一次；正文沿用稳定的短 event_id。通知失败不能回滚预测、成交或审批。

默认每次预测与结算静默；只发每日摘要、待审核策略、持续异常、启用与回退结果。

### 2.9 健康检查、运维与验证

healthz 只表示进程存活；readyz 检查数据库迁移、实例锁、各后台循环心跳、AgentRunner 和沙箱／模型配置状态。任一关键循环异常退出必须使 readyz 失败，不能只因 API 能返回就报告系统就绪。

外部每 5 分钟运行 `ta doctor --notify`，只做健康检查；runtime 停止时仍可独立发故障消息，不依赖故障数据库。相同错误每 24 小时最多提醒一次，恢复提醒一次。

macOS 用 launchd、Linux 用 systemd 管理 ta serve；异常退出后重启。结构化日志包含 request_id、job_id、attempt_id、fence、stage、forecast_id、duration_ms、error_code，令牌和凭据不写明文。日志保留 30 天；数据库每日备份，保留 7 份日备份与 4 份周备份，并做还原演练。

实施顺序：持久任务与唯一键 → 短事务领取／心跳／fence → 阶段提交 → AgentRunner 超时 → 启动恢复 → outbox → 健康检查和故障注入。

最低验收：

- [ ] 重复 tick、并发请求同一小时，只产生一个 job 和 forecast。（A01）
- [ ] 重复结算不重复记收益、费用、实验样本或账本事件。（A08）
- [ ] 领取后 A 失去租约、B 重新领取，A 的续租和结果写入均失败。（A07）
- [ ] forecast 提交后杀 CLI，恢复时不重新生成预测。（A06）
- [ ] 在每个阶段提交前／后杀 runtime，恢复只沿用已提交事实和冻结输入。（A07／A16）
- [ ] 旧 attempt 不能提交新报告；当前 job 已完成的趋势阶段能在重试时合法复用。
- [ ] 第二个 runtime 拿不到实例锁；持锁连接中断后原实例停止执行。（A07）
- [ ] Agent 超时后进程组退出、令牌失效、归属沙箱清理，API 和心跳未被等待阻塞。
- [ ] 发送失败不影响业务提交；超时 UNKNOWN 和重试保留同 event_id。（A13）
- [ ] 后台循环停止、数据库不可用或模型／沙箱缺失时 readyz 失败。（A18）
- [ ] 停机跨多个小时，缺失时段记 SKIPPED，已有预测全部继续验证。（A16）
- [ ] 从备份恢复后能查询原预测、证据和修订链，未完成任务可继续。

通过真实 PostgreSQL、可控模型传输与故障注入验证。恢复时间、重复业务写入次数、任务积压、循环延迟和 UNKNOWN 数量均为待测指标，不能把设计值写成实测成果。

待实施时进一步锁定：数据库事务／锁等待超时、循环心跳判定阈值、进程终止等待上限、通知完整退避和 UNKNOWN 策略。它们影响稳定性，但本次不虚构已确认参数。

主设计依据：第 2.1 节生命周期、第 3.3 节 AgentRunner、第 7.2 节任务表、第 11 节调度与恢复、第 12 节通知、第 14／16 节验收和运维。

### 2.10 设计核对记录与仍需证明的边界

2026-10-06 对照官方资料核对：

| 机制 | 资料支持什么 | 项目仍须证明什么 |
|---|---|---|
| 行锁与 SKIP LOCKED | 可用于队列领取时避开已锁行；不自动产生业务幂等 | 领取与更新在同事务，业务唯一键和 fence 判断完整 |
| Session advisory lock | 会话结束后释放；需要应用合作使用 | 专用连接、失锁检测与停止路径；失锁时仍存活的其他数据库连接不能让旧执行者继续接单／续租 |
| ON CONFLICT | 可以处理预期唯一键冲突 | 同内容复用、不同内容拒绝，并发冲突后的读取语义正确 |
| clock_timestamp | 反映实际时间，区别于固定事务开始时间 | 等待行锁后重查时间，过期心跳不能复活，晚到发布被拒绝 |
| 异步子进程 | 等待、通信、终止是不同操作 | 超时实际退出、后代进程处理、远程沙箱清理与接口响应 |
| Transactional outbox | 同事务记录业务和发送意图；外部投递可能重复 | 明确 UNKNOWN、重试与失败处理，通知失败不改业务结果 |

来源：[PostgreSQL 行锁与 advisory lock](https://www.postgresql.org/docs/16/explicit-locking.html)、[SKIP LOCKED](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE)、[AWS outbox 原理](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)。其他来源见对应实施章节。

资料证明的是底层机制的语义，并不能证明本项目的组合实现正确。特别是单实例锁连接丢失、旧任务续租和新实例接管的交叉时序，必须做真实数据库故障注入；仅测试「正常启动第二个实例会退出」还不充分。必要的实例接管门禁尚需在实现前细化，不能先承诺失锁时绝无并行执行。

本次补严：唯一键冲突的事务处理、过期租约禁止复活、数据库真实时间检查、超时后的进程／远程资源清理。这些均为设计修订，尚未有应用测试通过记录。

### 2.11 日志记录与任务追踪

日志需要回答：**这次任务由谁执行、走到哪一步、用了什么输入、哪里失败、是否已经提交业务结果。** 当前为待实现规格，不代表日志系统已经运行。

#### 三种记录分别负责什么

| 记录 | 存放位置与用途 | 保留 |
|---|---|---|
| 运行日志 | 宿主机结构化 JSON；查调用、耗时、缓存、错误、重试和资源清理 | 30 天，轮转限制磁盘占用 |
| 业务事实 | PostgreSQL 的快照、报告、预测、结算和 outbox 等；确定实际结果 | 长期保留 |
| 审计事件 | PostgreSQL audit_events；记录发布、审批、回退、导入等业务动作的操作者与引用 | 只追加，长期保留 |

业务成功由数据库已提交事实确定，不能用一条 INFO 日志代替。业务动作的 audit 与事实在同一事务提交；审计写入失败则该事务不能声称成功。运行日志缺失不代表业务未提交，服务恢复仍以数据库为依据。

#### 字段与关联

每条运行日志有 UTC timestamp、level、component、event 与简短 message；按当前场景附带关联字段，未知字段可为空，不能伪造。

| 场景 | 关联字段 |
|---|---|
| API 请求 | request_id、actor_type／内部 actor_id、route、status_code、duration_ms |
| 任务与阶段 | runtime_instance_id、job_id、attempt_id、fence、kind、anchor_at、stage、duration_ms、error_code |
| 输入与预测 | snapshot_id、bundle_id、trend_analysis_ids、forecast_id、engine_hash、strategy_id、输入／结果 hash |
| Agent 执行 | Hermes 父／子会话 ID、timeframe、provider／model、模型阶段、耗时、可获得的 token usage、进程归属与 sandbox_ref |
| 通知 | outbox_id、event_id、claim_id、发送 attempt、发送状态、可获得的 transport_message_id |

HTTP 请求、asyncio 后台任务和 AgentRunner 都显式传递关联 ID。实现建议使用标准库 logging、统一 JSON formatter 与 contextvars；子进程和插件边界显式携带安全的任务 ID，不能依赖进程内上下文自动跨进程传播。周期任务没有上游用户请求时生成内部 request_id。

#### 必记事件

| 环节 | 至少记录 |
|---|---|
| 生命周期与健康 | runtime 启动／就绪／停止、实例锁取得／失败／失去、关键循环退出与恢复 |
| 数据与上下文 | 行情请求结果、校验失败原因、冻结快照与 bundle、特征缺失及质量 |
| 趋势缓存 | 各周期命中、刷新、失效；收盘边界、原报告 ID、refresh_reason |
| 模型与任务 | 领取、新 attempt、模型开始／结束、schema 修复、超时、续租失败、旧 fence 提交拒绝 |
| 业务提交 | 发布／结算／审批等动作的请求、已提交结果、冲突或事务回滚 |
| 资源与恢复 | 进程终止／强制终止／退出确认、沙箱清理成功／失败、启动恢复决定与 SKIPPED 原因 |
| 通知 | 领取、发送成功、明确失败、UNKNOWN、下次重试或最终 FAILED |

成功提交的运行事件只在 COMMIT 成功后输出；回滚不能留下「已经发布」日志。进程可能在 COMMIT 后、写日志前崩溃，数据库事实和同事务 audit 用于补证。

正常续租和每次后台空轮询放 DEBUG 或采样，避免持续刷屏；阶段完成用 INFO，可恢复异常／拒绝用 WARN，任务失败或循环退出用 ERROR，并记录脱敏后的异常堆栈。日志级别与 error_code 一起使用，不能只靠一段自由文本统计故障。

一次超时排查的示意顺序：

```text
按 job_id 找到领取记录 → 查看 attempt_id / fence
→ 检查 snapshot / bundle 和趋势缓存事件
→ 找到模型阶段的开始、超时与提交拒绝
→ 查看进程退出、沙箱清理与重试／跳过
→ 查询数据库，确认到底有没有正式 forecast
```

#### 脱敏、写入与轮转

- 不记录 Bearer token、API key、数据库密码／完整 DSN、环境变量原文、人工凭据或完整 IM 目标。
- HTTP header、URL query、第三方错误和异常堆栈也必须经过统一脱敏；不能只过滤显式 token 字段。
- 默认日志记录输入／结果 ID 与 hash，不重复打印完整 K 线、提示词、模型回答、CSV 或整个请求 body；正式证据通过受限数据库引用查看。
- 模型 CLI 的 stdout／stderr 限长、脱敏后进入受限日志，不能无上限积存在内存。
- 日志输出与轮转不能长期阻塞 asyncio 心跳。实现时选择有界写入缓冲；低级别日志在过载时可限流或丢弃，但必须记录 dropped_logs 指标；关键错误的降级输出也要限长。
- 运行日志落在受限宿主机目录，可同时输出给进程管理器；轮转大小、文件数、总容量与日志队列容量在部署前锁定，不单靠 30 天保留规则控制磁盘。
- 日志磁盘满／输出失败需暴露健康或降级事件，不能递归记录错误把进程卡住，也不能把已经提交的业务改成未提交。

#### 日志验收

- [ ] 仅给出 job_id，就能串起所有 attempt、阶段、模型、缓存、业务结果和资源清理事件。
- [ ] 两个并发任务的 ID 不混串；没有上游请求的 scheduler 任务也有可追踪关联。
- [ ] 数据库事务回滚后，没有成功发布日志和 audit；COMMIT 后日志缺失仍能从业务事实恢复。
- [ ] 注入模拟凭据到 header、URL、异常文本和 CLI 输出，日志中无明文测试凭据。
- [ ] 缓存重复复用、旧 fence 拒绝、模型超时、通知 UNKNOWN 都有明确事件与原因。
- [ ] 日志队列过载、磁盘满和输出失败不造成心跳长时间停顿；丢弃数量与降级可见。
- [ ] 轮转后仍满足容量与保留设置，原始业务证据不随运行日志删除。

运行事件命名、错误码目录、日志容量和异常堆栈访问权限在实现前锁定；验收结果尚未产生。

## 三、沙箱与权限

### 3.1 先理解主体：执行隔离 + 业务授权

**让模型在临时环境里处理指定输入；正式业务写入由 runtime 检查权限和任务资格。**

```text
宿主机：runtime → AgentRunner → Hermes 推理编排与受信任插件
                                  ├─ terminal → trading-cube → 临时 CubeSandbox
                                  └─ research 插件 → 本地 API → 校验 → PostgreSQL
```

| 边界 | 实施目标 |
|---|---|
| 文件 | 输入只读、临时输出可写；宿主机源码、主目录和凭据不挂载 |
| 网络 | 正式预测用冻结输入，命令禁止出站；解释／复盘可访问公网，限制内网与管理地址 |
| 凭据与业务权限 | 模型不拿密码；插件只持有当前 attempt 的短期授权，审批留给 operator |
| 生命周期 | 每次 attempt 创建临时环境；结束、超时和崩溃后有归属明确的回收 |

父 Agent 与本次实际运行的子 Agent 共用一个沙箱和信任边界；按周期分目录是文件组织，不是子 Agent 之间的权限隔离。重试创建新的 attempt 和新沙箱；复用 12h 趋势只复制报告，不复用旧沙箱或令牌。

本章是实施设计。参考 Hermes commit `71cb7d60f8` 已提供接入接口；本项目 Cube 插件、权限中间件和下面的隔离测试尚未实现。

### 3.2 怎样接入 Hermes

在 `integrations/hermes/trading-cube/` 实现独立插件，通过 `register_terminal_environment_provider()` 注册 `cubesandbox`；不修改 Hermes 核心。安装插件后，第 2.2 节主设计中的 backend 配置才有效。

| 实现位置／契约 | 具体工作 |
|---|---|
| provider `create_environment()` | 创建固定模板，设置网络与资源策略，登记 sandbox_id；接受 Hermes 的 `task_id`、`container_config` 与扩展参数 |
| environment 命令执行 | 转接 SDK 的命令接口，固定执行用户和 cwd，处理 stdin、超时、输出上限与退出码 |
| 文件桥接 | 只上传当前任务的授权输入；下载结果限于当前 scratch，限制路径、文件类型与大小 |
| 进程与 cleanup | 对接远程进程终止、后台进程管理和环境销毁；重复清理保持幂等 |
| doctor／readyz | 核对模板、实际身份、网络与文件权限；接入失败阻止预测任务，不能改用 local 执行 |

provider 声明 `is_remote=True`、`is_container=True`、`session_isolated_when_nonpersistent=True`；使用 `strip_env_keys` 排除 Cube 认证信息，并独立检查实际 SDK 命令环境。`container_persistent:false` 控制临时会话语义，不代替清理和权限检查。

参考代码的原生委派会用 `register_container_alias(child_task_id, parent_task_id)` 将子任务指向父环境；插件需实际验证父子收到同一个 sandbox_id、不同 attempt 收到不同 sandbox_id。每个子任务使用自己的 cwd／scratch 子目录，不能依赖共享可变 cwd 推断命令执行位置。

建议继承 `BaseEnvironment`，适配 `_run_bash()` 返回的 ProcessHandle、终止和文件桥接契约。该 commit 的 `BaseEnvironment.execute()` 返回 `output`／`returncode`，插件指南的简化示例使用 `exit_code`；开发以调用源码为准，明确做 SDK 结果映射，不能只照抄示例就宣称兼容。

### 3.3 文件隔离：怎样落实输入只读

```text
/workspace/input/          # 当前快照、指标、bundle、授权趋势报告；只读
/workspace/scratch/parent/ # 父任务临时文件
/workspace/scratch/1h/     # 当前 1h 子任务临时文件
/workspace/scratch/4h/     # 仅本次需要更新时使用；6h、12h 同理
```

输入由宿主机受信任适配器上传，原件与 hash 留在 runtime。模型命令固定以模板内普通用户执行；建议输入目录与文件由 root 持有，目录 `0555`、文件 `0444`，且上级目录不可被命令用户替换。scratch 归普通用户所有；模板禁止该用户通过 sudo 或管理入口重新取得 root 执行权。

**不能只写一个 chmod。** 当前官方 SDK README 说明省略 `user` 时请求默认使用 root，因此插件必须显式选择普通用户，并检查模板内的提权路径、进程执行服务和文件服务能否被命令绕过。若普通用户能调用本机管理服务以 root 改文件，输入只读验收仍失败；此处需要模板和部署的真实测试。[SDK 依据](https://github.com/TencentCloud/CubeSandbox/blob/master/sdk/python/README.md)

不挂载宿主机项目、HOME、Hermes Profile、数据库目录、人工审核凭据、Docker／其他管理 socket，也不挂载上一次 attempt 的目录。命令可能在沙箱自己的 HOME、`/tmp` 写入缓存；这些位置须为一次性的沙箱内部存储，受磁盘限额约束。

文件桥接只接收服务端选定的输入文件，不能让模型传任意宿主机路径。下载仅允许当前 scratch 内的普通文件，禁止路径穿越、符号链接逃逸和任意宿主机目标；路径检查与读取之间也须避免链接替换竞态。Hermes 的缓存自动同步路径必须纳入检查，不能把关闭 file 工具当作已经阻止所有文件传输。

模型生成的 CSV、报告或 JSON 都是不可信输出。正式预测仍从结构化 API 提交，经 schema、输入引用与任务资格检查；不能直接执行输出文件，或把改过的沙箱副本当成行情原件。

### 3.4 网络隔离：按任务配置

| 任务 | 终端沙箱网络 | 宿主机工具 |
|---|---|---|
| 正式 1h 预测及本次趋势刷新 | 禁止出站，不设置公网 allow 例外 | 仅冻结数据与研究提交工具；不启用 web／browser／任意 URL 获取 |
| 解释／复盘等获准联网任务 | 允许公网，拒绝敏感内网与管理目标 | 可额外启用经过审查的搜索／网页读取工具 |

正式任务的 SDK 创建配置使用 `allow_internet_access=False`，不配置 `allow_out` 或 L7 目标例外；不能让模型通过工具参数选择更宽松的模式。依赖和研究脚本提前固定在模板中，执行期不靠下载软件补环境。

联网模式需核对宿主机地址、业务 API／数据库、Cube 控制面和云元数据目标，包括它们可能使用的公网 IP。官方出站策略的 allow 优先于 deny；不能再配置覆盖敏感目标的宽泛 allow。当前文档主要描述 IPv4／CIDR，部署还需确认 IPv6 被关闭或同样受控，不能凭 IPv4 拒绝规则声称所有协议都已隔离。[网络策略依据](https://docs.cubesandbox.com/guide/network-policy)

Hermes web 工具可能在宿主机执行，Cube 的终端出站策略不会自动覆盖它。宿主机网页抓取必须独立防止任意 URL、重定向或 DNS 解析访问内网；在完成检查前，不能把未经约束的抓取工具加入白名单。模型供应商请求由受信任 Hermes 编排发送，不需要向沙箱开放供应商接口或下发模型 API key。

远程 Cube 部署还要保护控制面与命令／文件数据面入口：runtime 经受保护通道访问必要服务，外部和沙箱命令不能获得管理权限。Cube API 默认无认证，需配置认证及网络访问控制；不能只在客户端放一个 key 就认定服务器已经验证。其他管理服务也按官方网络加固要求限制暴露。具体认证接法与 SDK 版本一起锁定。[认证依据](https://docs.cubesandbox.com/guide/authentication)、[部署加固依据](https://docs.cubesandbox.com/guide/network-hardening)

### 3.5 凭据与权限：谁能做什么

| 凭据／身份 | 所在位置 | 允许的动作 |
|---|---|---|
| 数据库应用凭据 | runtime | repository 与领域事务；不给 Hermes CLI |
| 模型调用凭据 | 受信任 Hermes 编排 | 调用锁定的模型；不给沙箱命令 |
| Cube 服务凭据 | 受信任 provider | 创建、执行与回收本项目环境；不给模型命令 |
| `research_attempt` 短期令牌 | 当前 CLI 的受信任研究插件 | 读取当前 bundle，按任务类型提交当前报告／预测／复盘 |
| `controller` 凭据 | 主控插件 | 查询与申请指定解释／复盘／实验任务 |
| `operator` 凭据 | 人工操作环境 | 审批、回退、成交导入与暂停／恢复 |

AgentRunner 构造最小 CLI 环境；研究令牌不出现在提示词、工具参数、命令、文件同步或日志中。终端 `env_passthrough:[]` 加上 SDK 显式环境白名单，避免 token、DB DSN、模型／Cube key 进入 guest；错误和 stdout 同样按 2.11 脱敏。

交易任务工具仅开放 `research_context`、`timeframes_analyze`、`forecast_submit`、`review_submit` 和沙箱终端／进程管理；实际提交能力还要按任务类型收窄。角色由服务端从凭据解析，忽略模型自报的 role；API 路由授权是硬检查，隐藏按钮或工具名不构成授权。

Hermes Profile 是配置与会话分离，**不是宿主机操作系统的文件权限隔离**。宿主机插件与管理员属于可信主体；本设计不声称能抵御恶意宿主机插件。人工凭据需要独立保管，不能放在可由模型控制的宿主机 terminal／file 工具读取的位置；主控的工具与终端也要检查，必要时使用独立系统用户或外部凭据存储落实边界。

### 3.6 短期任务令牌怎样生效与失效

实施建议采用 `secrets.token_urlsafe(32)` 生成不可猜测的不透明令牌；服务端只保存摘要，不要求模型处理 JWT。绑定 `job_id`、`attempt_id`、`fence`、任务类型与阶段、bundle、截止时间及撤销状态。

每次研究请求都执行：

```text
验证令牌 → 确定身份／任务／允许动作 → 校验当前 attempt 与 fence
→ 检查 lease、令牌有效期和任务状态 → 读取授权 bundle 或进入写入事务
```

写入时再按 2.4 的行锁和数据库时钟检查，避免请求进来时有效、提交时已经过期。正式预测令牌的到期时间不晚于本次执行硬截止和发布截止；任务完成、取消、超时、失去 lease 或被新 attempt 接管后立即不可用。有效期具体字段在 migration 中锁定。

「一次性」指令牌只用于本次 attempt，不是 HTTP 只能调用一次：上下文读取、分阶段报告和幂等重试需要多次请求。相同提交 hash 返回既有结果，不同内容按冲突处理。`GET /research/context` 由令牌定位 bundle，不接受模型指定任意 job_id／as_of；引用也必须属于该 bundle 的授权集合。

review 令牌不能调用 forecast 提交，research／controller 都不能批准策略；复制另一个任务 ID、给 body 加 `role=operator` 或拿旧 fence 请求，都不能扩权。结算和 outbox 继续由 runtime 内部调用，不发放模型可用的维护令牌。

### 3.7 限额、清理和崩溃回收

每次 attempt 创建前，先保存任务归属；创建时附项目标识、job／attempt／fence 等非秘密 metadata。返回后登记 sandbox_id、template／policy hash、创建时间与清理状态，可作为 `sandbox_ref` 的结构化内容或独立资源记录；具体存储结构在实现时确定。

CPU、内存、磁盘、进程数量和输出体积设有限上限，值由实际研究脚本压测后锁定。Hermes 的 container 配置必须映射到 Cube 实际资源，并通过查询与压力测试确认；未生效的限制不能当作已提供的能力。单条命令超时不能超过当前 attempt 的剩余预算；父子任务仍共同使用整批 120 秒上限。

正常结束或失败时：停止接受新调用／撤销授权 → 停止宿主机 CLI 及残留子进程 → 销毁远程沙箱 → 确认远程运行资源已终止 → 记录清理结果。子 Agent 结束只释放自己的命令／进程，不提前销毁仍由父任务使用的共享沙箱；最终销毁由 attempt 生命周期负责人执行。

Cube 的 `timeout` 是空闲超时，请求可能刷新它；它不能代替 120 秒整批墙钟上限。配置有限 idle TTL 与 `on_timeout=kill` 作为回收补救，同时保留 Runner 截止控制和启动恢复扫描。[生命周期依据](https://docs.cubesandbox.com/guide/lifecycle)

若创建成功但宿主机在保存 sandbox_id 前崩溃，恢复通过创建时的项目／attempt metadata 与数据库记录核对归属。只回收确认属于本项目、执行资格已失效的环境；归属不明先记录待人工处理，不能按一个命名前缀删除全部环境。

清理调用成功不等于资源已经终止；明确终止或不存在才记录成功。网络错误时保持待清理记录并重试，暴露积压数量；不能把清理失败抹成成功。清理数据库记录保留审计，临时计算环境不用于保存长期证据。

### 3.8 怎么实施与证明有效

实施顺序：**固定 SDK／模板与控制面访问 → 完成 provider 最小命令路径 → 落实文件／网络限制 → 接入令牌和 API 校验 → 跑真实隔离与恢复验收**。这些检查应在 M2 首条正式预测闭环前完成。

| 验收动作 | 必须观察到的结果 |
|---|---|
| 启动真实插件，执行研究命令 | backend 为 cubesandbox，普通用户身份，实际 CPU／内存限制可查询 |
| 修改、chmod、删除或替换 input；写入 scratch | 输入修改失败，scratch 成功；不能经 sudo／guest 管理服务绕过 |
| 放置宿主机测试凭据和其他 attempt 文件，再从命令与文件桥接读取 | 测试内容不可见；路径穿越、链接替换和缓存同步无泄漏 |
| 正式任务访问公网、内部 API／数据库、元数据与管理地址 | 访问失败；覆盖直接 IP、DNS／重定向及 IPv6 可用路径 |
| 获准联网的解释任务访问测试公网与敏感地址 | 公网成功，敏感目标失败；宿主机抓取工具同样受限 |
| 查环境、命令输出、异常和日志 | 无模拟 research／Cube／模型／operator 凭据明文 |
| 用 research／controller 访问审批；改 role 或另一个任务引用 | 服务端拒绝；不能读取其他任务或产生审批事实 |
| 超时、撤销、租约过期和新 attempt 接管后重放旧请求 | 无新业务写入；数据库中当前 attempt 结果保持正确 |
| 父子并行、子任务结束、父任务结束与重试 | 父子同环境且 cwd 不混串；子任务不误删父环境；重试为新环境 |
| 创建后崩溃、CLI 被杀、清理 API 暂不可达，然后恢复 | 确认归属的旧环境最终终止；未误删其他环境；失败与重试有日志 |
| Cube 或插件不可用 | 任务失败／就绪检查失败，无宿主机 local 命令兜底 |

每项留存版本、命令、实际结果、脱敏日志和资源／数据库证据；表格写的是预期，当前均未执行。不能只靠 mock 返回 403 或 SDK 创建成功就认定隔离有效。

开发前仍需锁定：SDK／服务／envd 与模板版本、普通用户与管理服务隔离方式、远程认证和受保护传输、完整网络目标与 IPv6 策略、资源／输出限额、资源记录与清理重试规则。无法满足输入只读或冻结网络要求时，停止正式预测接入并调整模板或适配方案。

## 四、策略迭代经验记忆

### 4.1 先看主体：何时给经验，Agent 怎样使用

**在策略迭代提出候选修改前给经验。** 先用当前成交证据确定问题，再搜索历史摘要，展开相关记录的完整正文，最后提出补丁并验证。经验帮助发现过去的做法、失败和代价；当前证据与固定测试仍是本次迭代的依据。

```text
已有 Markdown／已完成迭代 journal
        ↓ 程序提取摘要、分类并建立派生索引
legacy_strategy_memory_search → 最多 5 张 summary 卡 + recall_id
        ↓ Agent 选择相关经验
legacy_strategy_memory_read   → 冻结 content 分页
        ↓ 提出可验证的候选修改，携带可选经验引用
原迭代服务 → 固定测试 → 结果记录 + memory.json
```

这与 skills 的渐进式读取相似：先发现主题与摘要，再按需展开细节。正文是历史证据，不是可执行技能；不自动执行其中的命令，也不据此扩大权限。

| 记忆 | 用途与边界 |
|---|---|
| Hermes 原生 `USER.md`／`MEMORY.md`／会话 FTS5 | 稳定偏好与会话检索 |
| PostgreSQL 预测案例与 observation | 正式预测前按同 engine、同 1h 期限、决策前已结算条件召回，冻结进 `context_bundle` |
| 本节策略经验 SQLite | 代码迭代前按主题、关键词、路径和币种召回；没有同 engine／1h 期限限制，也未接入正式预测 |

本轮“积累经验”指把已有迭代组织成可追溯、可按需读取的资料；是否改善决策或收益还需后续实验。summary 是程序提取的有界文本，未调用 LLM 重新总结。

### 4.2 数据模型与来源可信度

实现位于 [ExperienceStore](../../../src/legacy_research/compat/experiences.py) 和 [输入契约](../../../src/legacy_research/compat/experience_contracts.py)。索引默认在 `workspaces/v1-compat/experiences.sqlite`，与 Hermes `state.db`、正式 PostgreSQL 分开；不增加数据库服务。SQLite 文件是可重建的派生数据，原始迭代记录才是事实依据。

实际 SQLite schema 版本为 `user_version=1`：

```sql
CREATE TABLE experiences (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    available_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    search_text TEXT NOT NULL,
    UNIQUE(source_key, version)
);
CREATE INDEX experience_source ON experiences(source_key, version);
CREATE VIRTUAL TABLE experience_fts
    USING fts5(id UNINDEXED, search_text, tokenize='trigram');
```

| 字段／层次 | 实际内容 |
|---|---|
| `summary` 列 | JSON 摘要卡：`experience_id`、短摘要 `summary`、`categories`、`classification_version`、路径、币种、状态、来源、版本、可用时点、hash、测试退出码与分模式样本量 |
| 卡片的 `summary` 文本 | 最多 320 个 Unicode 字符；旧记录取标题与“策略假设”内容，结构化记录取原假设 |
| `content` 列 | 原始 Markdown 全文，或规范序列化的结构化 JSON 字符串 |
| `source_key`／`source_hash`／`version` | 来源身份、派生来源内容 hash、同来源递增版本；内容变化追加记录 |
| `available_at` | 该版本首次被索引的 UTC 时间；不回填文件名日期或 mtime |
| `search_text` | 摘要与正文合并后规范化的搜索文本；检索全文，但第一阶段只返回摘要卡 |

摘要元数据向模型最多展示 8 个策略路径、10 个币种，路径展示最多 240 字符；`metadata_counts` 与 `metadata_truncated` 说明是否截取。完整标签仍用于数据库筛选，正文可继续分页读取。

来源处理如下：

- **旧 Markdown**：读取 `Strategy-iteration/*.md`，提取摘要、路径与币种。状态为 `UNVERIFIED_RECORD`，测试退出码和样本量为 null；不能把原文写着“测试通过”当成当前程序核验。
- **结构化 journal**：读取 `workspaces/v1-compat/iterations/<UUID>/` 的请求、补丁、固定测试、冻结成交证据与完成结果。校验 ID、请求 hash、补丁与原请求及源码 hash、证据快照 ID/hash 一致；未完成或未来完成记录不作为已完成经验。
- **终态保留**：`APPLIED`、`VERIFIED_CANDIDATE`、`TEST_FAILED`、`ROLLED_BACK` 都可进入索引。完整 journal 与其自动生成的 Markdown 只呈现一条经验。

结构化 content 保留假设、原始／候选源码、测试命令／退出码／输出／sandbox_id、风险、回滚、证据 hash、完成时点和 live／paper 分离统计。`APPLIED` 表示本机写回，候选表示测试通过，`deployed=false`；成交统计来自修改前，不证明修改后的收益。

### 4.3 分类与检索算法

分类版本 `iteration_topics_v1` 用固定中英文关键词扫描摘要和正文；一条经验可有多个标签，没有匹配才归 `general`。

| 标签 | 主题 |
|---|---|
| `entry_filter` | 入场、突破确认、冷却与准入 |
| `exit_management` | 止损、止盈、退出与跟踪 |
| `market_regime` | 趋势、震荡与行情讨论 |
| `position_sizing` | 仓位、保证金与杠杆 |
| `execution_safety` | 执行、订单、权限与安全 |
| `data_quality` | 数据、统计、资金费与采集诊断 |
| `general` | 无其他匹配主题 |

标签是文本主题，不是用 ADX、ATR 等计算的行情状态，更不代表经验适合当前市场。分类和检索均不调用 embedding 或 LLM。

每次搜索先读取授权来源，再在短 SQLite 事务内同步派生版本，最后检索：

1. `query` 最多 200 字符，经 NFKC 规范化与 `casefold` 后按空白分词；全部关键词都必须出现，重复词去重。
2. 长度至少 3 的词作为转义后的字面短语进入 FTS5 trigram `MATCH`；所有词另用参数化 `instr` 检查。长度 1–2 的词通过子串匹配，例如“止损”；输入不作为任意 SQL 或 FTS 运算符执行。
3. `categories`、`strategy_paths`、`symbols` 每组内任意匹配，组之间同时满足。币种转为大写、路径使用规范策略 Python 路径，两者精确匹配。
4. 只返回仍有来源且 `available_at <= decision_at` 的最新派生版本。有长关键词时先按 BM25 排序，再按可用时间倒序、ID 排序；空 query 浏览最近记录。
5. 返回 1–5 条上限内的摘要卡（默认上限 5）；零匹配返回空数组，附 `match_basis` 与可信边界。全文不会随搜索结果直接进入模型上下文。

SQLite 需支持 FTS5/trigram（SQLite 3.34+）；不支持时明确失败。没有关键词同义扩展、向量相似度、市场适用性评分或“检索质量已提高”的结论。

### 4.4 冻结、版本与迭代引用

搜索返回摘要时，将所选的**完整摘要与正文**冻结到 `workspaces/v1-compat/memory-recalls/<UUID>.json`，并保存 `recall_id`、本次 `decision_at`、查询和清单 hash。文件只追加写入；展开正文不再读取今天的索引内容。

| 操作 | 处理 |
|---|---|
| 展开详情 | 用 `recall_id` + 清单内 `experience_id`；校验清单 hash、经验 ID 与正文 SHA-256 |
| 读取长正文 | `offset`／`limit` 按 Unicode 字符计；默认 6000、最大 12000；返回 `next_offset`、`total_chars`、`content_format` |
| 结构化 JSON 分页 | 片段是文本；先拼接所需页再解析完整 JSON，单页不保证是完整 JSON 文档 |
| 同来源未变化 | 复用经验 ID 与版本 |
| 同来源有变化 | 新 ID、新版本，旧行保留；旧 recall 仍读旧正文 |
| 来源删除／索引重建 | 删除来源不再新召回；重建后索引 ID 改变，已有冻结清单仍可读取 |
| 重试本次迭代 | 复用原 recall 与经验 ID；不能重新搜索后替换同一次请求的历史引用 |

[IterationRequest 与指纹兼容](../../../src/legacy_research/reviews/contracts.py) 增加两个可选字段：

```text
memory_recall_id: UUID | null = null
memory_experience_ids: UUID[] = []   # 最多 5 个、不重复；非空时必须有 recall_id
```

[迭代服务](../../../src/legacy_research/compat/iteration.py) 在测试前核对清单时点与引用集合，追加保存 `iterations/<UUID>/memory.json` 中的引用全文，并将 ID 写进中文迭代 Markdown。清单外 ID、未来清单或 hash 不符均拒绝。无引用的旧请求计算指纹时移除新增字段，保持旧幂等行为。

hash 提供一致性检查，不是数字签名，也不能证明 LLM 理解了内容。本轮没有修改原始记录、自动合并或纠错；已实现的更新仅是**跟随来源变化追加派生版本**。尚无自动过期／退休或历史版本清理。

### 4.5 工具接入与权限限制

新增工具属于独立可选 [trading-compat 插件](../../../../legacy-trading/src/trading_system/telegram/__init__.py)：

| 工具 | 输入／返回 |
|---|---|
| `legacy_strategy_memory_search` | `query`，可选 `categories`（最多 7）、`strategy_paths`（最多 8，每条最多 240 字符）、`symbols`（最多 10）、`limit`（1–5）；返回摘要与 `recall_id` |
| `legacy_strategy_memory_read` | `recall_id`、`experience_id`，可选 `offset`、`limit`；返回冻结正文分页与下一页偏移 |

例如发现突破相关经验时传入：

```json
{"query":"突破","categories":["entry_filter"],"symbols":["BTCUSDT"],"limit":5}
```

CLI 对应 `ta --config <配置绝对路径> --json legacy memory-search --policy <兼容policy绝对路径> --request -`，从 stdin 接收查询 JSON；详情为 `legacy memory-read --policy <兼容policy绝对路径> --recall-id <UUID> --experience-id <UUID> --offset 0 --limit 6000`，同样使用全局 config 与 json 参数。安装配置和完整例子见 [操作说明](../../strategy-memory.md#使用)。

插件 hook 与 handler 都拒绝正式 `TA_ATTEMPT_ID` 和 delegated 子 Agent。管理员配置的 executable／config／policy 必须为绝对路径，CLI 校验兼容 policy 已启用；模型不能换路径或传任意 SQL。子进程使用最小环境，两个记忆工具不下发 Cube key、不调用 Cube 或模型。提示词 [legacy-iteration.md](../legacy-iteration.md) 引导先查摘要再读正文；六份正式预测／复盘提示词保持原流程。

资源边界采用：

- 独立非阻塞索引锁；未知数据库版本、索引错误和忙状态明确拒绝。
- 最多 2000 条索引输入；总读取量沿用 policy 的 `max_source_bytes`（默认 20MB）；单条材料／正文最多 2MB；冻结清单最多 12MB。证据快照可在总读取预算内读取。
- SQLite 文件权限为 0600，检查来源、清单、数据库及 sidecar 的符号链接。超额或非法资料不静默跳过。
- 索引、清单与本地 journal 已被 Git 忽略；部署备份范围需包含原始迭代记录及冻结引用，SQLite 可在无并发访问时重建。

这些工具只增加历史材料读取能力。现有策略源码白名单、新增测试限制、原测试不可改、成交证据与固定测试门禁继续生效；没有新增经验写入工具，也没有启用用户的 Profile 或 cron。

### 4.6 验证证据与后续边界

2026-10-10 的本地证据见 [strategy-memory-verification.json](../../strategy-memory-verification.json)。

| 验证 | 已观察到的结果 |
|---|---|
| 真实 SQLite/FTS5 与新增经验测试 | 中文长／短词、类别／路径／币种筛选、字面输入、摘要分层与分页正确；版本变化／来源删除后旧 recall 可读 |
| 拒绝与引用检查 | 篡改、清单外 ID、符号链接、大小超限、锁忙、未知 schema、非法迭代引用被拒绝；旧请求指纹兼容 |
| 固定 Hermes 原生插件 → CLI → SQLite | 实际工具可搜索／展开，正式 attempt 的 hook 和 handler 均拒绝；最终相关回归 37 项通过 |
| 44 份真实旧 Markdown 的隔离副本 | 建立 44 条索引；“突破”返回 5 张未核验卡并可展开；原运行状态未修改 |

完整旧框架回归 203 项通过。完整 v1 回归 185 项通过、1 项此前已复现的统计时间依赖失败、无跳过；后续收紧元数据与清单校验后，相关 37 项重新通过。上述结果验证本地实现，没有真实模型／Cube 新增联调、服务器启用或收益效果验收。

明确未实现：

- **经验自动编辑**：40% 样本重复率门槛、语义去重、LLM 自动新增／合并／纠错；需要后续先定义样本身份、重叠计算与修改审核规则。
- **经验有效性验证**：按行情特征匹配适用条件、修改后的收益归因、召回质量与决策效果实验；测试通过不能替代这些验证。
- **扩展与运维**：经验自动过期／退休、旧版本自动清理、接入正式预测、服务器部署及 Profile／cron 切换。

主设计第 6.5–6.8 节定义上述边界；本节提供实现依据。以后扩展经验写入或预测接入时，应先补契约、权限与验收，再开发新功能。
