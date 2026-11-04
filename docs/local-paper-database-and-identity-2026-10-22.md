> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 本机模拟交易数据库与内部身份配置

日期：2026-10-22。实际仓库 `/srv/open-trade`，本地开发记录 `/srv/open-trade-deployment/Feature.md`。

后续更新：已补齐独立只读备份账号、每日定时备份和独立空库恢复演练，见
[数据库备份与恢复交付](paper-database-backup-and-restore-2026-10-22.md)。下文验证结果与
“未配置自动备份”描述保留为首次建库时的记录；当前凭据目录另增加独立 `backup.env`。

用户明确授权在本机生成并保存新的数据库和内部身份凭据，当前仅用于模拟交易，OKX 保持只读。
已完成本机数据库初始化和凭据生成；完整预测交易 runtime 尚未启动。

## 数据库与账号

| 项目 | 脱敏配置示例（实际值已替换） |
| --- | --- |
| 独立数据库 | `example_paper`，PostgreSQL 16.13，编码 UTF8，时区 UTC，启用数据校验和 |
| TCP 地址 | `127.0.0.1:55432`，仅本机 IPv4 回环监听；按数据库与角色限制 HBA，密码采用 SCRAM-SHA-256 |
| 数据目录 | `.local/private-evidence`，专用系统用户拥有，权限 0700 |
| 数据库系统服务 | `example-paper-postgres.service`，已运行并设为开机启动；这是数据库服务，不是交易服务 |
| 系统用户 / 本地管理角色 | `example-db-owner`，系统账号禁止交互登录；管理仅通过受保护 Unix socket 和 OS peer 身份，无 TCP 管理入口 |
| 运行账号 | `example_app`，独立随机密码，不拥有数据库或表；无超级用户、建库、建角色、复制、绕过 RLS 或其他角色成员资格 |
| 迁移账号 | `example_migrator`，另一独立随机密码，拥有新数据库和迁移对象；也无集群管理权限，仅人工迁移环境使用 |
| 应用权限 | 32 张事实表仅 SELECT/INSERT；其他业务表按已有授权器授予 SELECT/INSERT/UPDATE；无 DELETE、建表、临时表或切换迁移身份权限 |
| 当前迁移 | `0009_prediction_knowledge`，50 张业务表，沿用既有迁移和事实保护触发器 |

本库没有复用 `55432` 的临时测试集群，没有迁移或导入旧行情快照、真实账户资金、持仓或历史交易。
数据库当前只有既有迁移生成的初始状态，没有发布预测、任务或模拟成交。

系统服务文件为 `/etc/systemd/system/example-paper-postgres.service`；PostgreSQL 专属配置为
`.local/private-evidence` 和同目录 `pg_hba.conf`。
服务沿用本机已有 PG16 二进制，不另拉数据库镜像；内存上限 512 MiB、连接上限 32、
共享缓冲 64 MiB，并限制为本机地址、专用目录和低权限系统用户。以上是本次自主部署选择，
没有修改交易风险限制或其他主机服务。

## 这些配置放在哪些 .env 文件

仍使用环境变量文件，但按用途分开保存。现有 `/srv/open-trade/.env` 的 OKX 凭据内容没有改动。

新凭据目录：
`.local/private-evidence`。
目录为 root 拥有、0700；下列四个普通文件均为 root 拥有、0600，并被 Git 忽略。

| 文件 | 包含的变量 | 加载者 |
| --- | --- | --- |
| `runtime.env` | `TA_DATABASE_URL`、`TA_CONTROLLER_TOKEN_SHA256`、`TA_OPERATOR_TOKEN_SHA256` | 未来唯一的模拟交易 runtime 服务 |
| `migration.env` | `TA_MIGRATION_DATABASE_URL` | 停止 runtime 后的人工迁移操作 |
| `controller.env` | `TA_CONTROLLER_TOKEN` | 主控桥接进程或人工查询 CLI |
| `operator.env` | `TA_OPERATOR_TOKEN` | 人工运维 CLI，用于暂停、恢复等已有操作 |
| 原有 `/srv/open-trade/.env` | 原有 OKX API Key / Secret / Passphrase | 宿主机只读行情客户端通过显式文件路径读取 |

两个内部令牌分别使用 `secrets.token_urlsafe(48)` 生成，独立于 OKX；runtime 只持 SHA-256
摘要。研究任务的短期令牌仍由既有程序按 attempt、lease、fence 和期限动态签发，本次没有
创建可长期通用的研究令牌。数据库密码分别使用 `secrets.token_urlsafe(36)` 生成。
本说明、Feature 和验证摘要不保存密码、连接 URL、令牌值或私有交易所原始响应。

应用不会自动扫描并加载这些文件。后续安装唯一 paper runtime 服务时，应使用：

```ini
EnvironmentFile=.local/private-evidence
```

当前尚未安装或启动该交易服务。`runtime.env` 暂只包含应用连接与两个摘要；Cube 管理鉴权
完成后再独立配置有效 `CUBE_API_KEY`。迁移连接和原始 controller/operator 令牌保持分离，
它们不能一起注入 runtime；研究模型和沙箱不获得这些静态凭据或 OKX 密钥。
主控身份只给主控进程使用，不能下发到研究子任务。

`trading-agent/.env.example` 是变量参考表，而非应该整体复制加载的部署文件。
非敏感交易参数继续放在现有 paper `config.yaml`；YAML 中只保留 OKX 凭据文件路径。

## 实际验收

使用新建库与实际运行账号验证通过：

- 应用密码登录、目标数据库、UTC、迁移版本和既有 `verify_app_permissions` 检查。
- 对全部 32 张事实表逐一尝试 UPDATE / DELETE / TRUNCATE，以及建表、删除运行状态、
  切换迁移身份，共 99 项 SQL 越权操作均返回权限拒绝；另验应用不能建库。
- 错误密码、连接其他数据库、管理员 TCP 登录均被拒绝；管理端确认两个密码均存储为 SCRAM。
- 正常 INSERT 审计记录、UPDATE 运行状态可执行，验证事务全部回滚；前后全部业务表行数和
  `runtime_control` 一致，没有把测试事实提交到模拟库。
- 两个原始内部令牌分别匹配自己的摘要与 controller/operator 身份，错误令牌不被识别；
  runtime 文件没有混入迁移连接或原始令牌。
- 原有 OKX 凭据、paper 草稿、执行配置和运行状态文件内容的前后摘要一致，行情采集服务仍 active。
  已验证的只读采集传输和原有 paper 执行模式保持原样，没有订单请求或全局风险变更。
- 正常重启新数据库服务后，上述全部验收再次通过，迁移、权限和数据状态保持一致。
- 新生成的密码、令牌值未出现在 Git diff、交付文档、Feature 或数据库服务日志中；凭据文件
  未追踪且被忽略，权限复核仍为 0600。

独立测试集群上的相关单元与真实 PostgreSQL 回归：**51 passed，131.42 秒**。
覆盖运维环境凭据隔离与日志、OKX 只读传输、事实追加/不可变保护、实际受限角色部署检查、
API 身份权限、幂等、任务租约/fence/期限及单 runtime 归属。执行位置
`/srv/open-trade/trading-agent`，设置仅指向隔离测试集群的 `TA_TEST_DATABASE_URL` 后运行：

```bash
.venv/bin/pytest -q tests/unit/test_operations.py tests/unit/test_authenticated_market.py tests/integration/test_storage.py tests/integration/test_deployment_identity.py tests/integration/test_api.py tests/integration/test_jobs.py
```

这些集成测试使用临时独立数据库和明确的行情/模型替身，与本机真实应用库验收分别记录；
不代替真实模型或交易验收。临时 PG16 测试集群已正常停止，持久模拟库继续运行。

本轮证据在 `.local/private-evidence`：
`provisioning-receipt.json`、`acceptance-*.json`、`final-hygiene.json`、`pytest-regression.log`。
其中部署脚本为一次性本机生成工具，重复执行会拒绝覆盖已存在的集群、服务或凭据。
实际验收可用以下命令复核；脚本只读取已生成凭据，所有探测业务写入均回滚，输出无敏感值的 JSON。

```bash
/srv/open-trade/trading-agent/.venv/bin/python .local/private-evidence
```

会话 `manifest.json` 只更新当前部署状态投影，移除了已完成的数据库/身份缺项，保留其余门禁；
旧文件原样备份为上述证据目录中的 `session-manifest.before.json`，行情和历史事实不变。
该投影继续标记 `runtime_started=false`、`BLOCKED_DEPLOYMENT`，不会启动新交易入口。

## 后续工作与回滚

数据库与内部身份这项已配置；后续仍需独立模型 Profile/认证、Cube 服务鉴权和完整隔离报告、
部署锁、通知/运行策略及首轮真实模型模拟联调。前轮耗时问题没有在本轮宣称已修复。
当前只运行数据库和原有只读行情采集，没有运行预测交易入口。
本轮未配置自动备份、独立备份身份或完成恢复演练；完整模拟正式启用前仍需落实这些运行事项。

本轮没有业务源码变化、没有 commit/push；现有未提交工作保留，Feature 保持 Git 忽略。

若只需停用本次新建数据库，可执行：

```bash
systemctl disable --now example-paper-postgres.service
```

此操作保留数据、历史、账号和凭据文件，不影响既有行情采集服务。
再次启用可执行 `systemctl enable --now example-paper-postgres.service`。
未来开始使用该库后应先正常停止唯一交易 runtime，再停库；不能把重建数据库当成回滚方式。
