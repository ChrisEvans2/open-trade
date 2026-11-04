> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 当前代码本地提交与验证记录

日期：2026-10-22（Asia/Shanghai）。实际仓库：`/srv/open-trade`。

## 提交范围与决定

用户要求先 commit 当前代码，本轮按仅本地提交、不 push 处理。依据实际暂存差异汇总此前多批未提交工作，不重新实现业务功能：

- 运行结构收敛：`trading-agent/` 为唯一业务 runtime；`packages/trading-core/` 提供共享风控、记账、执行和风险参数；量化实现仍由 `packages/trading-quant/` 提供。旧 CLI、研究服务、策略迭代与旧部署模板移至不安装的 `archive/`。
- 旧研究清理：移除旧测试及过时策略/交易日志文档，当前测试改为共享核心或独立事实夹具，不依赖归档代码；保留正式事实、九份迁移、历史读取与 Git 原件。
- 沙箱接入：现有任务运行期间有界续期，检查归属、授权、fence 和期限，失败终止并回收；兼容现有 Cube API、模板及文件桥，真实组件检查保持显式选择运行。
- 行情与备份：宿主机显式凭据文件支持 GET 白名单签名读取；模拟准备采集脚本追加快照；恢复工具在外部 pg_restore 前结束事务，同时保留跨提交的互斥锁。
- 主控恢复：保留 TG ⇄ Gateway ⇄ 主控 Hermes ⇄ 唯一 runtime ⇄ 独立预测 Hermes，恢复查询、受限解释/复盘任务和既有 outbox/HermesSender 通知，不新增网关或模型订单工具。
- 交付资料：纳入此前模拟准备、数据库/身份、备份、只读行情、部署清理、现有配置影响等说明，以及模块协作 HTML。说明中的历史操作和限制按原日期保留，不当作本轮重新执行。

本轮补建原先缺失的根目录 `Feature.md`，从当前差异和交付说明恢复未提交摘要；按实际暂存范围更新 `changelog.md`。`Feature.md` 继续被 Git 忽略。唯一源码整理是移除归档 `scheduler.py` 末尾多余空行，使暂存空白检查通过，无业务行为变化。

## 本轮复核

日志保存在 Git 忽略目录 `.local/private-evidence`。

| 验证 | 结果 |
| --- | --- |
| 根目录 `.venv/bin/python -m pytest -q` | 68 passed，2.06 秒 |
| runtime `TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/python -m pytest -q tests/unit tests/hermes/test_plugins.py` | 132 passed，47.76 秒；Hermes 模型/消息传输为测试替身 |
| runtime `.venv/bin/ruff check src tests integrations` | 通过 |
| 扩展跨目录 `.venv/bin/ruff check ../packages/trading-core/src ../scripts/collect_paper_market.py ../tests` | 未通过：74 条提示（52 E501、15 UP017、5 I001、1 B904、1 UP037）；未扩大到批量格式或异常处理改写 |
| 根目录与 runtime 各自 `uv lock --check --offline` | 均通过 |
| 暂存文件敏感特征与禁止路径检查 | 新增/修改文件未发现匹配；不包含 Feature、凭据、数据库、运行状态或本地部署包 |
| `git diff --cached --check` | 清理归档文件末尾空行后通过 |
| 隔离真实 PostgreSQL16 定向回归 | 28 passed，309.65 秒；覆盖六组回归，业务外部传输为测试替身 |

首次尝试复用已停止的历史测试集群，在临时端口 55432 启动后发现数据库角色与 OS 用户不一致。首轮 28 个连接错误均发生在 fixture 建库前，没有业务测试通过；只读角色探测也失败。没有更改历史角色或认证配置，已将该测试集群正常停止。

随后在本轮忽略目录下创建独立临时 PostgreSQL16，使用明确的测试管理角色和回环端口 55432；每项用例独立创建并回收测试数据库，不连接端口 55432 的模拟应用库。测试选取主控桥接、旧事实保留、预测进程/续期、结构事实备份恢复、分钟行情/到期持仓保护及确定性执行六组回归。

可复现的 PostgreSQL 命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
PATH=.local/private-evidence \
TA_TEST_DATABASE_URL=postgresql+psycopg://commit_test_admin@127.0.0.1:55432/postgres \
.venv/bin/python -m pytest -q \
  tests/integration/test_controller_bridge.py \
  tests/integration/test_runtime_consolidation.py \
  tests/integration/test_prediction_runner.py \
  tests/integration/test_backups.py \
  tests/integration/test_prediction_market.py \
  tests/integration/test_prediction_execution.py
```

该地址仅指向无密码的本机隔离测试集群，不能用于部署。最终 28 项全部通过，本轮三组测试共 228 项通过；已核对临时测试/恢复数据库全部回收，正常停止本轮测试集群，数据与日志保留。

## 边界、限制与回滚

本轮不启动或重启交易服务、不调用订单或发送 TG 消息、不启用实盘、不修改现有凭据或风险上限、不删除历史事实。临时测试库中的模型、行情和订单传输均为明确替身；真实数据库通过不能证明模型预测价值、模拟收益、真实成交或线上可用。

本轮未运行全部集成测试、真实 Cube 完整隔离、真实模型与 TG/Gateway 联调。扩展跨目录静态检查的 74 条提示保留在 `extended-ruff.json`；runtime 自身配置范围内的检查通过，不表述为全仓库静态检查通过。此前凭据预检/runtime 时间预算失败及高频调度锁竞争仍按原交付记录保留，不因选定回归通过而标记已修复。主控 Profile、TG 资料迁移、最终模型与沙箱配置、有效部署锁和持续在线验收仍须后续完成。

本地提交不发布或部署。若需源码回滚，反向提交本批代码与依赖并按相关交付说明核对既有工作；不得删除预测/评分/成交/审计事实或同时启动两个交易入口。此前运行操作的回滚分别参见其交付说明。本轮临时测试集群验证后正常停止，数据和日志保留。
