> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Hermes Profile 名称与运行环境目录

日期：2026-10-22（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户要求 Profile 名称不包含 `paper`，未来按角色沿用，当前模拟环境由路径和执行配置标识。

同日后续：用户已创建并配置 `prediction`，模型同步及插件安装情况见
[预测 Profile 接入说明](prediction-profile-setup-2026-10-22.md)。下文目录未创建状态是改名当时的记录。

本轮已修改生效的本机草稿
`.local/private-evidence`：

| 角色 | 原名称 | 当前名称 | 配置字段 |
| --- | --- | --- | --- |
| 独立预测 Hermes | `prediction-paper` | `prediction` | `hermes.trading_profile` |
| 主控 Hermes | `paper-controller` | `controller` | `hermes.controller_profile` |

`HERMES_HOME` 保持为：

```text
.local/private-evidence
```

后续创建的目录对应为：

```text
<上述 HERMES_HOME>/profiles/prediction/
<上述 HERMES_HOME>/profiles/controller/
```

当前两个 Profile 目录仍未创建，插件与模型认证也没有在本轮安装。此次是名称配置调整，
不把角色改名说成已经完成 Profile 部署。当前路径已有 `paper-trading` 标识，无需搬动目录、
数据库、行情或备份。未来使用其他环境目录时，可以继续沿用这两个角色名称。

## 后续手动配置模型

Profile 创建并安装相应插件后，使用 `--profile` 或 `-p` 选择角色。固定 Hermes 安装的
`model` 子命令用于选择模型/provider；例如预测 Profile 的命令为：

```bash
HERMES_HOME=.local/private-evidence .local/private-evidence --profile prediction model
```

主控则使用同一根目录和 `--profile controller`。必须在对应的 Hermes 根目录下选择角色，
不同根目录可以分别拥有同名 Profile。模型名称、认证和子任务委派最终确定后，仍需同步
runtime 的 provider/model 并生成最终部署锁。

Profile 名称不决定交易模式。本轮 `load_execution` 实际复核仍为 **PaperTransport**，
OKX 只读凭据、应用/迁移/备份连接、controller/operator 令牌、风险和执行文件内容均未改变。
没有启动 Gateway、模型或交易 runtime，没有新增实盘配置或调用订单。

## 变更与验证边界

- 实际草稿仅更改两个 Profile 字段，其他 YAML 数据和文件格式保留。
- 用同一代码计算改名前后部署清单，只有 `controller_profile`、`trading_profile` 两项变化；
  预测 engine identity 不变。指定部署锁尚未生成，无旧锁需要覆盖。
- 当前源码和配置示例本来就不含旧名称，没有为单台机器改动通用默认值或历史报告。
- 模拟应用库以 READ ONLY 事务复核：迁移仍为 `0009_prediction_knowledge`，受限权限有效，
  仍只有 `runtime_control` 初始化 1 行。没有改写已发布事实或账户状态。
- 部署/安装相关单元：8 passed，8.16 秒。独立 PG 部署身份用例首轮及复跑均遇到
  `ManagedProcess` 捕获已退出 Git 子进程时的 `psutil.NoSuchProcess`，分别为 24.94 秒、
  9.04 秒；这是同一个用例两次未通过，不能报告为 PG 集成回归通过。该进程管理源码与 HEAD
  一致，本轮未修改；问题留待单独修复。实际应用库 READ ONLY 的迁移、受限权限及记录量复核通过。

证据目录 `.local/private-evidence`：改前草稿、受保护文件
摘要、`rename-result.json`、`local-acceptance.json` 和相关测试日志。没有改写首次建库等
历史记录中的配置摘要；本轮经授权改变名称后，旧草稿摘要与当前值不同是预期变化。

验证命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
.venv/bin/pytest -q tests/unit/test_deployment.py tests/unit/test_runtime_packaging.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/integration/test_deployment_identity.py
```

数据库集成测试只使用独立临时 PG16/55432，不使用本机应用库 55432；行情/模型准备门禁使用
明确替身，不作为真实模型或完整部署证据。临时测试 PG 已正常停止。既有行情、模拟数据库与
备份服务没有为改名重启；未启动模型、Gateway 或完整交易 runtime。

## 记录与回滚

本轮按新指令记录在 `/srv/open-trade/Feature.md`，保持 Git 忽略；没有修改旧的
`/srv/open-trade-deployment/Feature.md`。未 commit/push，changelog 不提前归档。

回滚只需按证据目录的 `config.before.yaml` 对照恢复两个名称，保留用户后来对其他配置的修改；
不整份覆盖、不搬迁目录、不改凭据或删除历史。未来若 Profile 已经创建，应先停止其所属进程，
再单独核对目录与部署锁，不能仅恢复旧名字后继续启动。
