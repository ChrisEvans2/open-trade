> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

> 后续变更（2026-10-31）：父已调整为综合子报告，取消独立1h行情输入；子模型30＋1保持。以下保留当时的交付与验证记录，当前父输入以[父综合调整说明](parent-synthesis-inputs-2026-10-31.md)为准。

# 当前未收盘 K 线与临时指标接入

日期：2026-10-30（Asia/Shanghai）。范围：`/srv/open-trade` 本地实现，未部署。

后续补充：本日已继续实现复盘原始输入接入和分钟线历史重建，并完成配套包准备；当前技术行为和新增验证见 [复盘与分钟重建交付](review-candle-history-2026-10-30.md)。本文后面的“未新增重建”描述保留为本批最初交付的范围记录，以后续说明为准。

## 结果与用户确认

用户要求先解决当前 K 线被忽略的问题，随后明确：**复用现有指标公式，将当前未收盘 K 线加入计算，向模型提供带临时 tag 的结果。** 本次实现如下：

| 输入 | 使用数据 | 用途与标识 |
| --- | --- | --- |
| `model_bars` | 末尾 30 根已收盘 K 线 | 保留原模型历史视图 |
| `features`、`structures` | 原 80 根已收盘 K 线；外部指标沿用原观测规则 | 保留原指标、结构、参考价与验证路径 |
| `forming_candle` | 当前周期的一根未收盘 OHLCV 快照 | `confirm=0`、`status=PROVISIONAL`，保存请求/接收时间与周期进度 |
| `provisional_features` | **原 80 根已收盘 + 当前快照，共 81 根** | 复用共享公式，现有 26 项 OHLCV 指标逐项 `status=PROVISIONAL` |

例如北京时间 15:23 执行 4h 分析：80 根已收盘窗口结束于 12:00，模型同时看到 12:00—16:00 这根 K 线截至采集时的状态，以及包含该快照的临时 RSI、ATR、ADX 等数值。临时指标使用第 81 个观察值推进原公式，不丢弃最早一根重新初始化 Wilder 平滑。

本次没有新写指标公式，也没有添加现有清单不存在的 MACD。OI、资金费率、主动成交、市场宽度、强平与 BTC 相对指标不是由当前 OHLCV 直接生成；原先诊断出的缺失及 ETH 三项错误仍未在本次解决。

## 实际数据链路

1. `market/okx.py::forming_candle` 对实际分析标的与周期单次请求公开 `GET /api/v5/market/candles`，`limit=2`；1h/4h/6h/12h 使用既有 `1H/4H/6Hutc/12Hutc` 映射。
2. 只接受当前周期、`confirm=0` 的唯一一行。旧周期、未来周期、已收盘行不会充当当前快照；重复、非有限数、负成交量或非法 OHLC 不能成为有效快照。
3. `prediction/market.py::prepare` 完成历史 K 线与外部观测采集后再取当前快照。所有 HTTP 均在数据库事务外，沿用共享限流与本次 40 秒采集期限。
4. `prediction/service.py::freeze` 在原授权、lease、fence、期限检查事务内核验币种、周期、连续边界与可用时点，计算确认/临时两组值，并与快照一起写入同一不可变输入及 `content_hash`。
5. `prediction/submissions.py::model_context` 将快照、质量、临时指标及计算依据实际传给父/子模型。六角色提示词补充解释与复盘规则。它们属于现有 `market.current` 证据，不增加同一事实的重复引用。

完整快照内嵌在 `prediction_inputs.body`，不写进只保存已收盘事实的 `market_bars`。因此不需要数据库迁移，也不会让评分、结构确认或执行误读一根未收盘数据为最终 K 线。

代码入口（均在当前仓库）：

- [数据类型](../trading-agent/src/trading_agent/market/models.py)、[公开采集](../trading-agent/src/trading_agent/market/okx.py)。
- [公式适配与临时标记](../trading-agent/src/trading_agent/market/features.py)；公式仍由 [共享量化库](../packages/trading-quant/src/trading_quant/features.py) 唯一维护。
- [预测采集](../trading-agent/src/trading_agent/prediction/market.py)、[冻结事务](../trading-agent/src/trading_agent/prediction/service.py)、[模型视图](../trading-agent/src/trading_agent/prediction/submissions.py)。

OKX 的 `ts` 表示开盘时间，`confirm=0/1` 区分未完成/已完成；它没有提供这一 REST K 线行精确的最后成交更新时间。[OKX 官方接口说明](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks)

## 时间、数值与解释边界

- `requested_at` / `available_at` 是本地请求与接收时间。`elapsed_fraction_at_receipt` 是接收时已走过的周期比例，不是成交量完整度，也不代表交易所精确更新时间。
- 只有接收时间不晚于冻结决策时间、快照与最后已收盘 K 线紧邻、仍属于当前周期时才接受。快照请求距冻结不得超过已有的 `context_max_age_seconds`（默认 210 秒）。
- 采集跨周期边界会明确记录异常；如果历史窗口已落后于冻结时点要求，原输入校验会拒绝本次冻结，交给既有任务恢复机制重新取数，不混合两段周期。
- 临时特征的 `event_at` 使用快照接收时间，`available_at` 使用冻结计算时点，`source` 前缀为 `provisional:`。不会用未来的预计收盘时间冒充已发生事件。
- `close` 是快照采集时的价格，仍可能变化。`reference_price` 保留原已收盘参考价，提示词明确二者含义。
- RVOL、成交量 Z 分数仍按原公式计算；当前量尚未累满而历史量为完整周期。数值有效时额外标 `PARTIAL_COVERAGE` 及 `PARTIAL_VOLUME_VS_FULL_HISTORY_NOT_TIME_NORMALIZED`，不能据此确认缩量。没有擅自用线性外推代替真实成交量，也没有声称已实现历史同进度比较。
- `breakout_strength` 标明盘中、未获收盘确认；滚动 20 根 VWAP 偏离注明包含当前部分成交量。其他临时项也逐项保留 `PROVISIONAL`，退化值仍按原公式标记而不补零。
- 临时指标只计算配置所选的 OHLCV 项，不设新的必要指标交易门槛；结构候选、原确认指标、确定性触发/风险/执行校验保持原路径。

新增快照和26项临时值会增加模型输入体积。既有原生子任务入口在上下文超过30000字符时，会要求通过 `research_context` 读取完整授权输入，保留该路径而不截断指标。本次未复跑真实模型的耗时、分析质量或线上验收。

父综合当前取其 1h 尺度快照，各子预测在实际运行时取自身周期快照。仍有效的子预测继续按已有机制复用，其历史快照不会静默刷新。不同任务不是同一瞬间采样，但所有输入都记录各自真实可用时间。

## 缺失与历史保留

公开端点返回空或没有合格当前行时，`forming_candle=null`，质量为 `MISSING`，原因为 `CURRENT_CANDLE_NOT_RETURNED`。超时、拒绝、格式错误等可识别采集故障保留对应原因码。此时临时指标为空，原已收盘分析仍可继续；不复制最后一根已收盘 K 线、不补造当前价格。程序内部错误和范围校验错误不会被统一吞掉。

新增字段按 `candle_context_version=1` 标识；冻结输入正文和预测输出保留 `schema_version=3`，模型内容契约保留 4，以兼容既有独立周期入口及父子轮次工作流。旧输入原文和 hash 不改，模型读取缺少新字段的旧记录时显示 `LEGACY_INPUT`。

同一任务重试沿用已冻结的快照与临时值。另一新任务可以保存更新后的当前快照；以后收到该周期最终 K 线，也不会覆盖此前输入。回放应读取原冻结快照；没有快照的旧历史不能用最终 OHLCV 回填来假装当时已知。现有定期复盘行情仍为每周期 80 根已收盘数据，不新增全历史盘中重建系统。

## 自主实现选择

1. 使用独立类型和独立输入字段，保留确认数据的强校验；不放宽 `Bar` 对收盘时间的约束。
2. 使用 80+1 根复用公式，原 80 根确认值同时保留；不将全部 37 项外部/价格特征强行重算为临时值。
3. 快照只在实际预测准备阶段抓取一次；没有新增常驻订阅、独立调度器或实时刷新循环。
4. 在既有 JSON 冻结事实中内嵌快照和结果，避免额外可变状态；缺失作为可见的辅助输入缺口。
5. 量能公式数值保留，另标部分覆盖与未校正原因；没有扩展同进度历史采集。
6. 保持父子预测复用、参考价、结构确认与交易规则；本次没有引入新的盘中交易策略。
7. 在新建的回环端口 55432 PostgreSQL 16 集群测试，只创建/清理 pytest 临时数据库，不连接业务 55432 或原验收库。不启动交易 runtime。

## 验证

验证记录保存在忽略目录 `.local/private-evidence`。

- 相关单元与打包：**66 passed，16.43 秒**，日志 `unit.log`。
- 新增 PostgreSQL 专项：**9 passed，55.99 秒**，日志 `integration.log`。
- 最终契约兼容复测（新增专项 + 预测 API）：**12 passed，85.02 秒**，日志 `compatibility-final.log`。重复项不累计为新增覆盖数。
- 较广预测回归：**73 passed、1 failed，537.27 秒**，日志 `regression.log`；唯一失败为下文已在本批前隔离源码复现的启动器夹具问题。最终契约兼容检查另跑上述12项，不重复累加覆盖数。

- 单元覆盖：BTC/ETH 四周期端点映射与确认标记、15:23 时间实例、空/最终/旧周期/未来/重复/非法/NaN 行、跨收盘边界、26 项共享公式一致性、确认值不受污染、未来时点拒绝及临时量能提示。
- 真实 PG 覆盖：父子共 10 种角色/标的/周期组合实际 prepare→freeze→model_context、冻结 hash、80/30 数量与 81 根计算依据、可见缺失、旧输入读取、错误标的/周期/未来/过期快照拒绝、重试不重绘、新快照及最终 K 线不覆盖原事实。
- 公开行情：北京时间 2026-10-30 13:25:44 完成 BTC/ETH×四周期 8 组探测；每组 80 根已收盘、1 根当前未收盘、26 项临时结果。合计 17 次无凭据公开 GET（1 次时钟、8 次历史、8 次当前）。每组 24 项 OK，2 项量能 PARTIAL_COVERAGE；26 项全部有数值并带 PROVISIONAL。证据 `public-probe.json` 与脚本 `public_probe.py`。这是实际公共数据与本地计算验证，不是线上模型或交易验收。

回归中的 `test_runner_preserves_rejection_instead_of_no_formal_result` 有已有夹具不兼容：`configure_runner` 仅提供模拟 executable，而前一批未提交的启动器已要求 `python_executable`，故提前返回 `HERMES_NOT_CONFIGURED`，尚未执行到测试想验证的拒绝原因分支。在忽略目录构建移除本批改动、保留先前批量持久化和启动器改动的独立源码副本；同一测试仍报完全相同错误（8.04 秒），实际导入路径已核对。当前代码单独复测也相同（8.47 秒）。证据为 `recovery-baseline.log`、`recovery-check.log`。没有回滚工作区、修改业务启动规则或把此项计为通过。

初次新增 PG 测试错误地要求连接池占用为零；测试 runtime 的单实例锁会常驻占用一个连接。已改为核对相对该基线没有事务连接占用，同时更正测试的冻结 hash 字段名。未因测试错误调整业务事务边界。

复核命令（测试工作目录为 `/srv/open-trade/trading-agent`；`TA_TEST_DATABASE_URL` 仅指向本次 55432 隔离测试集群）：

```bash
.venv/bin/python -m pytest -q tests/unit/test_forming_candle.py tests/unit/test_market.py tests/unit/test_quant_owner.py tests/unit/test_prediction_market_client.py tests/unit/test_observations.py tests/unit/test_statistics.py tests/unit/test_breadth.py tests/unit/test_runtime_packaging.py
.venv/bin/python -m pytest -q tests/integration/test_forming_candle_inputs.py tests/integration/test_prediction_market.py tests/integration/test_prediction_pipeline.py tests/integration/test_prediction_recovery.py tests/integration/test_analysis_rounds.py tests/integration/test_review_market_windows.py tests/integration/test_prediction_api.py
.venv/bin/python -m pytest -q tests/integration/test_forming_candle_inputs.py tests/integration/test_prediction_api.py
```

公开探测从仓库根目录运行 `trading-agent/.venv/bin/python .local/private-evidence`。修改的 9 个 Python 文件 Ruff check / format check、`git diff --check`、文档本地链接及六角色实际提示词加载检查通过。

## 上线与回滚边界

本次只完成本地开发与验证。未启动实盘、调用真实订单、修改凭据/风险限制、部署或重启服务；没有改写历史预测、评分、成交与旧冻结输入，未 commit/push。

后续部署需要按现有流程重新生成匹配源码、六角色提示词 hash 与部署锁的包，不能把本地修改视为已上线。回滚应只撤本批新增快照/临时指标接入和提示词，保留此前其他未提交工作及所有已保存事实；新字段可由旧读取路径忽略，不能删除历史输入。测试临时集群收尾停止，保留本次证据。
