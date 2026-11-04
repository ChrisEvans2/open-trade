> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 旧研究测试与过时日志清理

日期：2026-10-22（Asia/Shanghai）；唯一仓库 `/srv/open-trade`。
用户在结构收敛后明确要求删除旧研究测试和不符合当前结构的交易日志。

后续更正：用户明确要求保留主控 Hermes/Gateway/TG 交互和独立预测 Hermes。
本页末尾曾移除 trading_job_create 的处理现已撤销，改为接入当前结构任务，详见
[主控链路恢复](controller-hermes-restore-2026-10-22.md)。旧测试/日志清理继续保留。

## 已清理的工作区内容

- 删除 `/srv/open-trade/trading-agent/tests/history/` 及旧 MarketService/MemoryService 的观测测试，共 16 个旧研究测试源文件。
- 删除 `/srv/open-trade/archive/legacy-trading/Strategy-iteration/` 与 `trade-strategy-logs/` 下共 45 份过时策略/交易日志 Markdown。
- 删除仅用于旧研究测试的 3 份归档插件副本（bridge/cube/research）；保留当前正式插件。
- 删除旧 Advisor、技术入场策略、Telegram 和旧调度入口的测试；仍适用的执行、风险、资金费、记账检查改为直接测试 `trading-core`。
- 删除 pytest 的归档源码/旧测试路径，清理 Hermes 探针里不再执行的旧研究分支，移除 pandas 及其开发依赖。

删除文件清单和内容摘要见 `.local/private-evidence`；
根目录旧测试的替换清单见同目录 `removed-root-tests.json`。已追踪原件仍可从 Git 历史恢复。
这些删除是本次后续授权，替代前次“先归档保留旧测试/旧日志文件”的工作区安排。

## 保留并迁移的当前检查

- `tests/test_execution_core.py`：直接检验执行器、reduce-only、保护单及拒绝响应，全部使用明确替身。
- `tests/test_accounting_core.py`：直接检验 AccountBook 的资金费单次结算、浮盈、成交事件与进出场路径；不再写旧 JSONL 文件账本。
- `trading-agent/tests/integration/test_metrics.py`：通过 v2 API 和结构任务检验原生用量权限、去重、冲突及缺失计数。
- 通知测试使用当前 PredictionService/JobService，备份恢复测试使用当前结构预测和验证事实。
- 历史读取/旧任务拒绝测试只构造合成的数据库事实夹具，检验当前 runtime 的向后兼容保护；不再启动或导入旧研究程序。

正式 PostgreSQL 预测、评分、交易、审计、数据库迁移和只读历史接口没有删除或改写。
当前行情快照、模拟数据库及前轮开发验证证据未清理。旧源码/规格/审计报告仍为非安装归档资料，当前测试不再依赖它们。
没有删除原始私有成交事实或向本机应用库写入测试数据。

## 验证

| 检查 | 实际结果 |
| --- | --- |
| 当前共享核心回归 | 68 passed，2.35 秒 |
| 当前 runtime 单元回归 | 129 passed，18.55 秒 |
| 当前完整测试收集 | 211 tests collected，无缺失旧模块/夹具错误 |
| 迁移后通知、用量、结构备份及历史保留的真实 PostgreSQL 回归 | 8 个不同用例通过；首轮 7 passed / 1 failed，最后失败用例修正断言后 1 passed，12.38 秒 |
| 当前 Hermes 插件契约 | 3 passed，24.48 秒，使用固定本机 Hermes 与明确模型替身 |
| 静态与依赖检查 | 当前 runtime src/tests/integrations Ruff、相关格式、根目录新/改测试静态检查、两份锁离线检查、当前文档链接、git diff --check 均通过 |

PG 首轮唯一失败是新合成评分夹具的断言依赖了字符串尾零：接口返回 `0.26`，原断言要求
`0.260000000000`。改为 Decimal 数值比较后通过；没有修改评分或序列化程序。
全部不可变事实表的内容摘要在历史读取与恢复前后保持一致，真实 pg_dump/pg_restore 验证结构预测和验证事实可以恢复，且恢复后仍拒绝改写事实。

所有数据库检查仅使用回环 55432 的独立测试 PG16，不连接 55432 的本机模拟应用库。
本轮使用的测试集群已正常停止，本机模拟 PG 与行情采集服务仍 active，没有重启或修改它们。
模型/行情/订单替身不能视为真实交易或收益验收。前次高频调度锁竞争限制没有在本次声称修复。
测试数量与前次含旧策略的回归不同：本次删除旧入口/旧算法断言，并迁移仍适用的共享核心检查；没有通过改交易逻辑来减少测试。

复核命令（PG 连接仅指向隔离测试集群）：

```sh
# /srv/open-trade
.venv/bin/pytest -q
uv lock --check --offline

# /srv/open-trade/trading-agent
.venv/bin/pytest -q --collect-only
.venv/bin/pytest -q tests/unit
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/integration/test_metrics.py tests/integration/test_outbox.py \
  tests/integration/test_backups.py tests/integration/test_runtime_consolidation.py
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q tests/hermes/test_plugins.py
.venv/bin/ruff check src tests integrations
uv lock --check --offline
```

备份测试需 PostgreSQL 16 的 pg_dump/pg_restore 在 PATH 中。实际日志、删除清单与验证摘要位于
`.local/private-evidence`，保留失败和成功证据。

## 记录与回滚

保留此前所有未提交改动；变更前文件副本和差异位于 `.local/private-evidence`。
本轮不启用交易、不调用真实订单、不改变凭据或风险上限，不操作交易服务，不 commit/push。
Feature.md 继续忽略，changelog 不提前归档未提交内容。

回滚只恢复本批指定文件：用本轮前副本恢复改写的当前测试，以 Git 原件及前轮路径清单恢复
需要的旧文件；不能批量 reset/clean 或覆盖其他任务改动。没有数据库迁移或事实变更需要回滚。

## 同日后续补漏：移除主控失效任务工具

用户追问后确认：前次退役了后台 `POST /v1/jobs`，但遗漏了主控
`trading_job_create` 的工具注册与 manifest。此前这条入口可见，调用时会返回
`410 LEGACY_FLOW_RETIRED`；前次插件回归没有覆盖此遗漏。

本次沿原清理授权移除 `trading-bridge/__init__.py` 中的注册、专用 POST 处理器与无用
`uuid4` 导入，并从 `plugin.yaml` 删除工具名、将插件说明改为查询功能。不新增替代任务
API，不重新启用旧研究流程。当前插件保留九项 GET 查询，包括 v2 结构预测/交易意图和
历史任务、预测、结算、实验、复盘；当前自动预测复盘不受影响。

扩展现有固定 Hermes 原生发现探针：检查退役工具不被发现、manifest 与实际注册一致，
并分别使用 v1/v2 配置前缀调用保留的九项查询。验证结果：

- 固定 Hermes 原生插件三种模式、runtime 安装边界与部署单元：11 passed，36.51 秒。
- 真实隔离 PostgreSQL 16/55432 的旧写拒绝、旧队列恢复、历史预测/评分/审计保护：
  3 passed，40.30 秒；全部不可变事实表摘要保持一致。
- 修改源码与探针 Ruff、格式检查、文档链接、`git diff --check` 通过。

命令在 `/srv/open-trade/trading-agent` 运行：

```sh
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q tests/hermes/test_plugins.py tests/unit/test_runtime_packaging.py tests/unit/test_deployment.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/integration/test_runtime_consolidation.py
```

测试使用合成身份、回环 HTTP、明确模型/沙箱替身和独立真实 PG；不代替真实模型或线上验收。
独立测试集群已正常停止，没有连接或改动 55432 的本机模拟应用库。

变更前副本和本轮日志保存在 `.local/private-evidence`。
本次仅清理仓库源码，未安装或刷新 Hermes Profile、未部署或重启交易服务；已生成旧
bundle 不随源码自动更新，正式接入前仍需按最终配置重新生成 bundle 与部署锁。
不修改 API、数据库迁移、正式事实、凭据或风险限制。未 commit/push，其他未提交工作保留。
回滚仅按本轮 `before/` 副本恢复对应文件并核对后来改动，不清库、不批量覆盖工作区。
