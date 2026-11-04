> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 项目结构收敛与历史保留

日期：2026-10-22（Asia/Shanghai）；实际仓库 `/srv/open-trade`。
本批本地开发与定向验证已完成，未 commit、push 或部署交易服务。

> 后续清理：用户随后要求移除旧研究测试和过时日志。tests/history、旧框架入口测试及 45 份策略/交易日志文档现已从工作区删除，pandas 开发依赖和归档测试导入路径已移除。下文原测试结果为收敛阶段证据；最新范围与验证见 [后续清理交付](obsolete-research-cleanup-2026-10-22.md)。

用户授权：更新唯一入口说明，提取仍被复用的交易核心，再收拢旧流程和过时文档；保留历史预测、评分和审计。

## 最终结构

| 位置 | 当前职责 |
| --- | --- |
| `/srv/open-trade/trading-agent/` | 唯一 `ta serve` runtime：结构预测、父综合、验证、执行、知识复盘和期限学习 |
| `/srv/open-trade/packages/trading-core/` | 共享交易核心：确定性风控、原风险参数、仓位账本、退出计算、执行客户端和社媒风险辅助 |
| `/srv/open-trade/packages/trading-quant/` | 唯一指标与结构几何计算实现，未复制量化算法 |
| `/srv/open-trade/config/` | 继续沿用的风控、交易参数、执行模式和社媒风险配置 |
| `/srv/open-trade/archive/` | 旧交易框架、旧预测研究、策略源码迭代、旧提示词和历史说明；不安装、不部署 |

根 `pyproject.toml` 是开发工作区，不再发布旧应用或安装 `trade-agent`。
生产 runtime 直接依赖 `trading-core` 和 `trading-quant`；pandas 在后续旧测试清理中也已从开发依赖移除。

## 入口与文档清理

- 两份 README 已按当前结构重写，明确 `ta --config <配置文件> serve` 为唯一业务入口。
- 原 `trade-agent cycle/daemon`、Advisor 服务模板和通知脚本移入 `archive/legacy-trading/`；不再由生产安装提供。
- 旧小时分类 `forecasts/reviews/memory`、旧研究调度器、旧 AgentRunner 和策略代码迭代兼容实现集中到 `archive/legacy-research/`。
- 生产插件移除 `forecast_submit` 和旧两阶段委派；仅保留独立结构周期、父综合、知识检索与预测复盘工具。`trading-compat` 不进入生产资源或部署 bundle。
- 旧模型提示词、旧部署/运行逻辑、v1 整合和旧实现状态移入归档。当前需求和实现文档保留，并修复归档链接；旧路径变化在需求文档顶部注明。
- 旧策略/交易日志文档曾集中归档，后按用户新要求删除；Git 原版本及审计 JSON 保留。
- 旧研究测试最初归档，后按用户要求移除；当前默认 pytest 收集 unit/integration/Hermes，不再导入归档夹具。真实 Cube 验收仍需显式 opt-in。

前次收敛时曾核对 158 个已追踪移动文件的新位置及历史文档字节；其中后来明确删除的旧日志文件见后续清理清单。
完整路径清单保存在 `.local/private-evidence`。
归档资料见 [历史索引](../archive/README.md)。Git 状态中的旧路径删除对应归档/核心中的新路径，不是删除历史事实。

## 交易核心的实际提取

`trading-core` 无 CLI、调度循环、技术入场策略或 Advisor 方向裁决。

1. `accounting.py` 的 `AccountBook` 从旧 `TradingScheduler` 提取资金费、成交应用、仓位标记、盈亏及移动退出相关方法。原账本计算保留一份；14 个提取方法的 AST 与原实现一致（忽略格式），归档调度器回归继承这一实现。
2. `prediction_adapter.py` 直接使用 `AccountBook`，不再实例化旧调度器。调用方继续通过现有受保护 PG 事务保存账户和事件；核心不另写文件账本。
3. `parameters.py` 的 `TradeParameters` 与 `policy.py` 从旧入场策略/CLI 中提取风险参数和配置读取。保留原参数默认值、类型转换和冻结参数字段，避免修改风险口径和账户策略指纹；不实例化技术入场策略。
4. `risk/`、`execution/`、签名客户端、账户数据结构和社媒风险辅助转入共享核心。运行时仍由确定性执行服务调用传输，模型不获得订单工具。
5. 执行配置不支持原 ATR/区间参数时，继续返回 `RISK_ADAPTER_UNSUPPORTED`；没有退回另一套策略或默认风险配置。

`legacy_config_dir` 配置键暂保留为既有配置兼容名，仍指向相同配置目录，不表示可启动旧 runtime。
原风险配置、趋势/震荡倍率、最大可承受损失、仓位上限和到期继续持仓规则均未调整。

## 唯一 runtime 与历史访问

- `prediction.mode` 仅接受 `structural`；Runtime 构造也拒绝 legacy 模式。
- runtime 只建立结构预测、知识、期限学习和执行服务，不构造旧研究服务。领取任务仅限当前 PREDICT_*、KNOWLEDGE_INDEX、HORIZON_LEARN。
- 旧 `POST /v1/jobs` 和旧研究上下文/提交接口在认证后返回 `410 LEGACY_FLOW_RETIRED`，先拒绝再解析请求体；不生成旧任务或旧预测。
- `/v1/forecasts`、`/v1/reviews/{id}`、`/v1/jobs/{id}` 等历史 GET 保留原义。`ta review show` 提供只读复盘查询，移除旧 review request、legacy、成交导入和策略审批的 CLI 入口。
- 启动/恢复时，更新遗留未完成旧任务的状态投影：有已发布结果的保留成功结果；不再可执行的任务标记 SKIPPED/LEGACY_FLOW_RETIRED，撤销相关旧 attempt 的运行权限。只更新任务、attempt 和调度状态，不重算旧预测/评分。
- PostgreSQL 仍保存正式事实，SQLite/FTS5 仍为派生索引。全部九份迁移和历史事实表保留，无模式迁移或数据清理。

历史读取在独立 PG 测试中验证：先通过归档实现构造历史预测、结算分数及审计，再用当前 API 读取并执行恢复；全部不可变表内容摘要前后相同。
这仅为测试夹具，不是向本机模拟应用库注入历史数据。

## 验证结果

| 验证 | 实际结果 |
| --- | --- |
| 根目录交易核心及旧框架回归 | 207 passed，14.69 秒 |
| 当前 runtime 单元回归 | 128 passed，18.86 秒；最后参数拒绝兼容/安装检查另跑 3 passed，其中 2 项与前者重叠 |
| PG 预测、知识、行情、期限学习、AgentRunner、任务、存储、身份和沙箱生命周期 | 52 passed，376.27 秒 |
| PG API、执行、历史与安装检查首轮 | 16 passed / 1 failed；失败是原 15 秒结构启动等待，包含安装单元检查及后续重跑的历史用例 |
| 调整测试调度频率后的启动与历史最终回归 | 4 passed，69.65 秒；包含完整启动闭环及 3 项历史保留检查 |
| 固定 Hermes 当前插件契约 | bridge/structural/cube_cancel：3 passed，24.09 秒；模型传输为明确替身 |
| 三组件 wheel 构建、独立安装、资源检查 | 通过；生产包无 trading_system、legacy_research、trading-compat 或旧 CLI；含 6 份结构提示词和全部 9 份迁移 |
| 部署包准备 | 从独立 wheel 安装生成 bundle，含 3 个当前插件；未安装到 Profile 或启动服务 |
| 锁文件、静态检查、链接和空白 | 两份锁离线校验通过；当前源码及新增测试 Ruff/编译检查通过，当前文档相对链接无缺失，git diff --check 通过 |

最终通过的不同 PostgreSQL 用例共 68 项，不将重复运行和单元安装检查重复计入。
使用本轮独立 PG16、回环端口 55432；逐测试创建/删除临时数据库，没有使用 55432 的本机模拟应用库。
本轮新建测试集群最终停止，数据和日志保留。最初误用旧测试集群的管理角色导致连接失败的日志同样保留，未修改旧角色或凭据。

核心命令：

```sh
# 仓库根：含原风控/记账和归档策略回归
.venv/bin/pytest -q

# trading-agent：只指向独立测试 PG，不能填本机应用库
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/unit
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/integration/test_prediction_pipeline.py tests/integration/test_prediction_scheduler.py \
  tests/integration/test_prediction_market.py tests/integration/test_prediction_knowledge.py \
  tests/integration/test_review_market_windows.py tests/integration/test_horizon_pipeline.py \
  tests/integration/test_prediction_runner.py tests/integration/test_jobs.py \
  tests/integration/test_storage.py tests/integration/test_deployment_identity.py \
  tests/integration/test_cube_lifecycle.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/integration/test_prediction_api.py tests/integration/test_prediction_execution.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/integration/test_structural_runtime.py tests/integration/test_runtime_consolidation.py
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q tests/hermes/test_plugins.py
```

详细日志、保留性校验、wheel 清单与路径映射在
`.local/private-evidence`。

## 已知限制与未做事项

- **高频调度锁竞争未解决。** 原启动夹具每 50–200 毫秒轮询，首轮 15 秒及诊断 60 秒均超时，并出现 runtime_control/jobs 锁等待。最终启动夹具改用 scheduler=5s、executor=0.2s、notifier=1s、recovery=2s，保持并发并等待完整事实；这验证正常启动，不证明原高频压力下的问题已修复。生产调度、锁超时、模型预算、任务期限和预测期限均未改变。
- 模型、行情替身和真实 PG/本机进程分别记录。本批未运行真实模型预测、交易所模拟订单或真实订单，没有新增准确率或收益结论；未声称完整线上验收。
- 旧 CLI/服务在其他环境的既有安装不会被代码归档自动卸载。本批仅更新开发依赖，不切换机器服务；部署前仍需确认同账户只有一个入口。
- 插件和包布局变化后，需要重新生成部署锁、构建并验收部署产物。已有部署 bundle/锁保留作历史，未自动覆盖。
- 本批运行相关单元和定向 PG/Hermes 回归；没有把全部历史流程或所有当前集成用例重跑一遍，也不将未运行的检查写为通过。

## 自主决定与既有工作保护

必要技术配套包括：独立核心包与根开发工作区、旧模块非安装归档、认证后的旧接口退役、旧排队任务恢复、只读复盘 CLI、测试分组和独立安装验证。没有新增交易策略、Agent、数据源或风险权限。

本轮前未提交文件已逐文件保存到 `.local/private-evidence`。
配置草稿、Cube 插件、沙箱实现、日志及相关原有测试中的 6 份文件与本轮前字节相同；其余既有只读客户端、factory、AgentRunner 和续期测试在保留原行为基础上适配新核心/新任务，未覆盖已有工作。
旧生成的 ai-trading-system egg-info 移入本轮忽略证据目录，避免空 src 目录留下过时入口元数据。

未修改凭据、全局风险、实际持仓、历史评分或机器交易服务；未重启行情采集或本机模拟数据库。
Feature.md 按规则继续保留在忽略范围，只记录尚未提交工作；本批未提前写入已提交 changelog。

## 回滚

仅恢复本批路径移动和源码/锁文件变更，参照路径清单及本轮前快照逐文件核对，保留其他未提交内容。
恢复原锁后重新安装本地依赖；若未来已部署本批，先停止唯一 runtime，再按验证过的包与部署锁回滚。
本批没有数据库模式或生产状态迁移，不通过清库、删除事实、延长旧预测期限或启动旧服务回滚。
保留全部 PG 数据、SQLite、快照、Git 中的原始策略记录、失败和成功的验证证据。
