> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 预测 Hermes 模型接入与后续配置

日期：2026-10-22；仓库 `/srv/open-trade`。本轮接续用户手动配置模型后的本机模拟准备。

后续更新：Cube 服务端鉴权、20 项真实隔离和预检修复已完成，见
[最新交付](cube-isolation-and-preflight-2026-10-23.md)。下文待办保留为当时记录；主控/TG、最终部署锁和真实模型模拟闭环仍待完成。

## 已确认与已完成

- 独立预测 Profile 名称保持 **`prediction`**，主控名称保持 **`controller`**；`paper` 只用于当前环境目录和模拟执行配置。
- 用户配置的预测模型为 **`openai-codex / gpt-6-luna`**，推理强度 **`high`**。运行目录为
  `.local/private-evidence`。
- 已将相同模型、端点和推理强度同步到本机 runtime 草稿
  `.local/private-evidence`。
- 已把仓库现有 `trading-research`、`trading-cube` 插件安装到此 Profile，并配置研究工具白名单、
  Cube terminal、父子 Agent 模型设置和规范稳定偏好文件。固定 Hermes 原生发现两插件均成功，
  Cube provider 已注册；没有 attempt 所属沙箱资源时仍不可用，不回退本机 terminal。
- 使用此 Profile 的现有认证进行预检，成功；再完成一次真实、无工具的模型连接检查，
  返回模型确为 `gpt-6-luna`，`high`，状态 completed，约 20.343 秒。请求只要求回答 OK，
  没有使用行情、账户资料、交易工具或数据库，不是一次正式行情预测。

认证文件仍由 Hermes 管理；本轮未人工修改模型认证、OKX 凭据、数据库连接、内部令牌或风险配置。
没有启动完整交易 runtime、Gateway，没有发送 TG 消息，没有真实订单。既有行情采集、数据库和备份
服务保持运行，没有为本轮重启。

## 下一步的实际顺序

| 顺序 | 仍需完成 | 当前证据 |
| --- | --- | --- |
| 1 | Cube 管理鉴权、受保护访问、完整隔离与资源验收 | 本轮不带认证读取本机 `/sandboxes` 仍返回 HTTP 200；尚无完整真实隔离报告，`isolation_verified=false`。不能把已有沙箱组件测试当作正式验收。 |
| 2 | 排查真实 runtime 预检耗时与进程竞态 | 独立认证预检两次成功，约 6.337 / 5.473 秒，当前模型检查只给 1.5 秒；运行后 readiness 的部署检查外层还只有 2 秒。之前命名任务的快速 Git 子进程 `NoSuchProcess` 竞态也未修复，本轮 PG 身份用例通过不能证明竞态消失。 |
| 3 | 迁移主控 Profile 和 TG 配置，绑定当前 runtime | 主控代码已由另一 session 恢复；本机 `controller` 目录仍不存在，`notification.target` 为空。复用现有 Gateway、trading-bridge、outbox/HermesSender；不另建网关。 |
| 4 | 配置完整后生成部署锁，验证基础模拟闭环 | 逐项通过后验证真实预测→确定性风控→模拟成交→事实保存→复盘；当前未生成正式部署锁，未绕过门禁启动。 |

Cube 与主控准备可分别推进；最终整体验证需要两者齐备。步骤 2 应修复检查的预算/缓存或启动机制，
并验证进程归属安全；不能简单放宽模型、预测发布期限或沙箱授权期限来掩盖问题。
TG 仍依赖用户计划中的旧服务器资料迁移。本轮不替用户猜测 bot、chat target 或已有 Cube 管理凭据。

## 必要的源码配套与自主决定

用户明确要求的是：接入既有模型、保留两个 Hermes 角色、继续完成模拟配置。本轮必要配套包括：

1. `HermesConfig` 增加可选 `model_base_url`、`reasoning_effort`，Profile 生成器保留手动选择的设置，
   父子 Agent 使用一致设置；认证仍只留在独立 Profile。端点不得含 userinfo、query、fragment，
   避免把认证信息带入配置、engine identity 或部署清单。
2. 这两项实际模型设置纳入 engine identity 和规范 Profile hash，之后更改会使旧部署锁失效。
   未设置两项时不向旧 engine identity 增加 null 字段，保留已有未显式配置者的 identity。
3. 规范 Profile 写入固定 Hermes revision 对应的 `_config_version: 48`，保留用户原有 schema stamp，
   避免首次 CLI 迁移补 stamp 导致 Profile 与部署锁不一致。完整 Profile hash 因此需要重新生成部署锁；
   本机原本就没有最终锁，未覆盖已有锁。
4. 本轮配置文件继续 0600，保留用户后续手工配置能力；插件只从当前仓库复制，不安装归档流程。
   稳定偏好遵循现有部署规范；SOUL、认证和 `.env` 保留原样。

未增加新的业务 Agent、网关、数据库或交易入口。通用 `trading-agent/config.yaml` 只增加两个可选
字段示例；用户具体模型与本机路径留在 Git 忽略的本地配置中，不将私有认证提交仓库。

## 验证结果与限制

- `tests/unit/test_deployment.py`、`tests/unit/test_runtime_packaging.py`、
  `tests/integration/test_deployment_identity.py`、`tests/integration/test_prediction_runner.py`：
  **20 passed，51.38 秒**。其中真实 PostgreSQL 用例 6 项；使用独立 PG16/55432，测试结束后已停止。
  模型、市场和沙箱替身只用于测试程序边界，不作为真实预测/隔离验收证据。
- 新增测试覆盖模型端点/推理强度改变 engine identity、Profile 偏离锁后被拒绝、凭据不能嵌入端点。
- 原生 Hermes 合约测试 **3 passed、2 failed，38.00 秒**：3 项插件发现/委派/取消测试通过；
  `test_credentials.py` 的无凭据/有凭据两个用例均超过各自 5 秒 subprocess 上限，其中有凭据用例
  已输出 configured=true 后仍未及时退出。未修改测试上限、未将失败记为通过；独立真实认证预检和
  真实模型连接成功不能替代此测试结果。认证预检预算及退出耗时留待后续修复。
- Ruff 检查、格式检查、`git diff --check` 通过。
- 实际应用库 PG16/55432 只读复核：迁移 `0009_prediction_knowledge`，50 张业务表，
  仅 `runtime_control` 初始化 1 行；本轮没有写入伪造预测、订单或收益。
- 本机保护文件前后核对包括根 `.env`、五份独立数据库/内部凭据、Profile 认证/`.env`/SOUL 以及执行风险配置。

真实模型连通、正式结构预测成功、风控与模拟交易成功、预测准确度和收益分别报告。本轮只证明
所选真实模型可调用、配置已同步和插件能加载，不能宣称整套自动模拟已上线或有效盈利。

验证命令（工作目录 `/srv/open-trade/trading-agent`）：

```sh
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q \
  tests/unit/test_deployment.py tests/unit/test_runtime_packaging.py \
  tests/integration/test_deployment_identity.py tests/integration/test_prediction_runner.py
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q \
  tests/hermes/test_plugins.py tests/hermes/test_credentials.py
```

本机证据目录：`.local/private-evidence`，
包含配置改前备份、保护文件摘要、模型连接布尔/耗时结果、原生插件发现、就绪检查及测试日志。
公开交付不包含原始私有交易所响应、认证内容或模型完整请求响应。

## 记录与回滚

本轮工作记录于 Git 忽略的 `/srv/open-trade/Feature.md`，不修改另一个部署目录的记录，
未 commit/push，也未提前归档 changelog；保留先前未提交的 Profile 命名说明。

回滚时对照证据目录的 `runtime.before.yaml` 和 `profile.before.yaml`，逐项恢复本轮模型字段及
Profile 配置；移除本轮新增的两个插件目录和两份规范稳定偏好文件，保留用户后续变更、SOUL、
认证与历史文件。源码仅回退本轮差异。不得整份覆盖后来改动、删除历史业务事实或变更凭据。
