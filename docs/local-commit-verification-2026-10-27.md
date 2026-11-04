> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 当前代码本地提交与验证

日期：2026-10-27（Asia/Shanghai）；仓库 `/srv/open-trade`。

## 范围与本轮决定

用户要求 commit 当前代码。本次将 10 月 22 日上次提交之后积累的代码、部署配套、测试和交付说明一起做本地提交，不 push，不部署或重启业务服务。依据实际暂存差异将根目录 `Feature.md` 汇总到 `changelog.md`；Feature、凭据、数据库、运行状态和本机部署产物继续不进入 Git。

- 预测工作流：新增 `0010_prediction_workflows` 迁移及轮次、角色运行、事件、双模型名额持久状态；按币种在固定 30 秒窗口合并事件。复用有效子预测、单角色有限重试、error 退避恢复与父预测可用性均受数据库授权、租约、fence 和期限约束。
- 原生执行：同一父会话使用固定 Hermes 的公共子执行生命周期，四周期模型可并行；父综合实际分析 120 秒、阶段 150 秒，整轮 1200 秒，新输入预算 210 秒。父等待子任务单独计时；取消未确认的资源保留占用并由宿主收束，不重置次数上限或伪造发布。内容引用由程序绑定正式 ID，发布事实仍追加保存。
- 预测恢复：保留可信提交拒绝、细化安全反馈和冻结引用；需要时创建新输入任务，提交前重新验证分钟数据、当前子预测及持仓；正式发布事实优先于进程退出/失败收尾，历史结果不重写。早期单任务恢复规则与最新轮次规则按实现范围分别保留。
- 通知：自动上下文刷新保存失败和承接事实，停止逐任务重复通知；过期 SENDING 转 UNKNOWN 且不自动重发，明确失败仍有界重试。无法确认送达的消息可能未送达，不承诺恰好一次或必达。
- 模型、权限与预检：可配置模型端点/推理强度；独立预测 Bot 仍复用既有 Gateway，TG 聊天与 CLI 研究工具分离。认证有独立预算、缓存及后台刷新；修复已退出短进程的身份观察与回收竞态。
- Cube 与部署：纳入本机边界、guest 令牌隔离、paper-only 启动守卫、内存参数样例和真实隔离/容量探针；单命令取消保持兄弟命令，子工作目录隔离。所有真实组件测试仍须显式选择执行。
- 交付资料：纳入此前 Profile 配置、CLI 入口、TG 迁入、代理/登录恢复、旧工作流的实际模拟部署、通知修复、Cube 内存与合成容量验收等历史说明。本次未重做这些操作，不以历史闭环证明最新工作流已部署。

详细业务设计、工程选择及文件映射见 [预测工作流技术路线](prediction-workflow-technical-route.md)。本次自主决定仅为本地提交范围整理、隔离验证和记录归档，保留已有实现与业务规则。

## 本轮验证

证据目录：`.local/private-evidence`，不提交日志或测试库。

| 检查 | 本次结果 |
| --- | --- |
| runtime 全部单元与固定 Hermes 契约 | 165 passed，67.56 秒；153 项单元与 12 项原生契约 |
| 隔离真实 PostgreSQL16 定向回归 | 58 passed，363.44 秒；本轮共 223 项测试通过 |
| runtime `ruff check src tests integrations deploy` | 通过 |
| runtime `ruff format --check src tests integrations deploy` | 通过，130 个文件 |
| 根目录/runtime 各自 `uv lock --check --offline` | 均通过 |
| 边界脚本 `bash -n` 与内存参数 JSON 解析 | 通过，仅验证语法，不应用配置 |
| 暂存敏感特征与禁止路径筛查 | 未发现匹配；不输出任何疑似秘密值 |
| `git diff --cached --check` | 通过 |

命令在 `/srv/open-trade/trading-agent` 执行：

```bash
TA_HERMES_REFERENCE=.local/private-evidence \
  .venv/bin/python -m pytest -q tests/unit tests/hermes --tb=short

PATH=.local/private-evidence \
TA_TEST_DATABASE_URL=postgresql+psycopg://commit_test_admin@127.0.0.1:55432/postgres \
  .venv/bin/python -m pytest -q \
  tests/integration/test_analysis_rounds.py \
  tests/integration/test_analysis_execution.py \
  tests/integration/test_prediction_scheduler.py \
  tests/integration/test_backups.py \
  tests/integration/test_structural_runtime.py \
  tests/integration/test_outbox.py \
  tests/integration/test_prediction_recovery.py \
  tests/integration/test_deployment_identity.py --tb=short
```

PostgreSQL 为本轮目录中新建的专用测试集群，无密码地址只用于本机测试；每项用例独立创建临时数据库并迁移至 0010，不使用模拟应用库或修改既有账号。最终确认临时测试/恢复库已全部回收（剩余 0 个），测试集群正常停止，保留日志和集群数据。

## 真实边界与回退

本次未调用真实模型、TG 发送或订单接口，未重跑真实 Cube 隔离/容量、全部 PostgreSQL 集成或收益验收；Hermes 契约真实加载固定原生组件，但模型/API/沙箱和消息传输使用明确测试替身。早期失败与成功证据均保留在原交付文件，不将不同版本、重复运行或 collect-only 混算为本次通过。

最新工作流要求业务库先迁移至 0010 并授予匹配权限，再安装一致代码、提示词、Profile 与锁文件；本次没有对业务库迁移或加载新代码。真实双模型/八子负载、真实取消、持续运行及新流程预测价值仍待后续验收。此前单工作流的真实模拟闭环和合成双沙箱容量不替代这些项目。

源码可按本次提交反向恢复；若未来业务库已迁移或运行新代码，应先暂停新分析并保留全部事实，使用匹配 0010 的修复版本向前恢复。迁移不提供删除事实的 downgrade，不回灌旧备份丢弃新事实，不启动第二交易入口。本次不启用实盘、不修改凭据或全局风险限制、不删除历史预测、评分、成交和审计。
