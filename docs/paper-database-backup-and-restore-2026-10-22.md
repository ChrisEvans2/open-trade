> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 本机模拟数据库运行配置：只读备份与恢复验收

日期：2026-10-22；实际仓库 `/srv/open-trade`。
用户要求本 session 继续数据库等配置，另一 session 恢复主控 Hermes/Gateway。
本轮负责数据库运维，不改主控、预测、模型、Cube 或交易入口的配置。

## 已完成

已有应用库、运行账号、迁移账号和内部令牌继续使用，见
[前轮配置说明](local-paper-database-and-identity-2026-10-22.md)。本轮补齐：

| 项目 | 实际配置与结果 |
| --- | --- |
| 源数据库 | `example_paper`，`127.0.0.1:55432`，迁移 `0009_prediction_knowledge` |
| 新备份数据库角色 | `example_backup`，独立随机密码，连接上限 2，默认事务只读；仅本模拟库 CONNECT、public USAGE、表和序列 SELECT，无管理权限或角色成员资格 |
| 后续对象权限 | 为 `example_migrator` 在 public 下将来创建的表/序列设置 SELECT 默认授权给备份角色 |
| 新备份系统用户 | `hermes-paper-backup`，禁止交互登录，独立于数据库系统用户和 runtime |
| 备份凭据 | `.local/private-evidence`，root:root，0600，仅含 `TA_BACKUP_DATABASE_URL` |
| 备份目录 | `.local/private-evidence`，备份系统用户拥有，0700；dump 与校验文件均为 0600 |
| 服务和定时器 | `hermes-paper-backup.service`、`hermes-paper-backup.timer`，文件位于 `/etc/systemd/system/`；定时器已启用并等待下一次执行 |
| 计划 | 每天 UTC 02:40，即北京时间 10:40；保留 7 份日备份，周日另保留周备份，最多 4 份；沿用现有工具策略 |
| 首份真实备份 | `daily-2026-10-22.dump`，153,655 字节，配套 `daily-2026-10-22.sha256`；由实际非 root 系统服务完成，Result=success、ExecMainStatus=0 |
| 恢复演练 | 首份备份已恢复到独立测试 PG 的新空库，50 张业务表的内容、迁移版本和触发器与源库一致，重新应用受限权限后仍拒绝修改预测事实 |

本轮只在 `pg_hba.conf` 为这个备份角色增加一条仅本机、仅源数据库的 SCRAM 规则，执行 reload，
没有重启 PostgreSQL。原 `.env`、四份运行/迁移/主控/人工操作凭据，以及 paper/风险配置
内容前后摘要一致。`backup.env` 没有并入 `runtime.env` 或任何 Hermes Profile。

源库当前仍只有 `runtime_control` 的 1 条初始化记录，没有真实模型预测或模拟成交。
因此，首份实际备份演练证明当前真实数据库可恢复；非空结构预测和验证记录的恢复能力另由
明确标记的真实 PG 集成夹具验证，不把两类证据混为已经开始交易。

## 独立备份运行环境与必要修补

现有项目 Python 链接到 `/home/operator/.local/private-evidence`，低权限服务无法穿过 `/root`。
本轮没有放宽 `/root` 权限，也没有让备份服务以 root 运行，而是：

- 将不含凭据的 Python 3.11.17 发行版复制到
  `.local/private-evidence`；代码由 root 拥有并供备份用户读取执行。
- 在 `.local/private-evidence` 创建专用工具环境，离线安装现有
  `trading-core` / `trading-quant` 构建产物和备份工具所需依赖，随后安装包含本次恢复修补的
  新 runtime wheel。服务只执行 `python -m trading_agent.storage.backups`，不启动业务 CLI。
- 新 wheel 在 `.local/private-evidence`；其用途为本次
  备份工具快照，不代表另一 session 正在恢复的主控代码已经完成或成为正式交易部署产物。
  业务部署仍须等待最终代码和配置，再重新构建并验收。
- 验证专用环境中的 `storage/backups.py` 与当前修补后的源码字节相同；未改变业务 venv。

恢复工具原本在空库检查事务中调用外部 `pg_restore`。本轮在启动外部进程前提交检查事务，
同时继续由同一连接持有 **session advisory lock**。互斥锁跨 commit 保留，恢复结束或失败后
连接关闭释放；仍要求目标库名明确一致、新建空库、校验和正确，以及事务化恢复。
没有降低非空库拒绝、单 runtime 或事实不可变保护。

扩展现有真实 PostgreSQL 恢复测试，另开连接观察恢复开始前检查连接为 idle、xact_start 为空，
并确认第二个连接取不到互斥锁，再运行真实 pg_restore。这是修复数据库事务与外部 I/O 边界的
必要配套，不是交易功能变更。

新服务使用 `ProtectHome=yes`、只读系统文件、仅备份目录可写、PrivateTmp/PrivateDevices、
非特权系统用户、256 MiB 内存上限及 25% CPU 配额。服务依赖顺序指向本机实际的
`example-paper-postgres.service`；没有配置会自动启动交易的依赖。

## 实际验收与证据

证据目录 `.local/private-evidence`，仅本机 root 可访问。

- `provisioning-receipt.json`：初次账号/权限/工具环境/系统服务配置，包含构建产物摘要，不含密码。
- `acceptance-*.json`：真实备份角色登录、全部业务表与迁移表仅 SELECT、无管理员和其他角色
  成员资格；即使主动关闭默认事务只读设置，实际 UPDATE/DELETE/TRUNCATE/建表/切换角色
  五类尝试仍被 ACL 拒绝。原 controller/operator 令牌仍分别匹配自己的摘要。
- 实际备份文件校验和匹配，权限为 0600；原服务 PID 保持不变。
- 首份真实备份恢复目标：临时测试集群 `127.0.0.1:55432` 中新建
  `example_paper`。没有把测试连接指向应用库 55432，也没有覆盖已有库。
  备份内容与源库前后快照相同，恢复后的事实保护触发器保留，受限角色授权可重新应用；
  再次尝试向同一非空目标恢复会被拒绝。
- 真实恢复副本和测试集群数据保留，临时 PG 已正常停止；不自动删除复制来的历史事实，
  该库不能作为交易运行目标。Pytest 自己创建的合成夹具库按原测试生命周期清理。
- `pytest-backup.log`：相关单元与真实 PG **18 passed，24.54 秒**，其中 16 项单元、2 项 PG。
  PG 覆盖非空结构预测/验证事实恢复、不可变保护、非空目标拒绝、检查事务与互斥锁边界，
  以及受限运行角色的部署检查。行情/模型/仓位均为明确夹具，无真实订单调用。
- 新备份定时器已启用；当前安排的下次触发为 **2026-10-23 02:40 UTC**。
  首轮是人工启动同一个系统服务完成的真实备份，尚未到次日自动触发时点。

相关回归命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
PATH=<本机PG16工具目录>:$PATH TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/unit/test_backup_retention.py tests/unit/test_operations.py tests/integration/test_backups.py tests/integration/test_deployment_identity.py
.venv/bin/ruff check src/trading_agent/storage/backups.py tests/integration/test_backups.py
.venv/bin/ruff format --check src/trading_agent/storage/backups.py tests/integration/test_backups.py
```

实际空库恢复使用了本轮修补后的专用工具环境。密码没有放在命令行或 PGPASSWORD，既有工具
仅为 pg_dump/pg_restore 生成临时 0600 pgpass 文件，并在结束后清理。原始凭据、私有响应
和数据库行内容未写入交付说明或 Feature。

## 使用、交接与仍未完成事项

查看本机备份状态，不需要读取或打印凭据：

```sh
systemctl list-timers hermes-paper-backup.timer --all
systemctl show hermes-paper-backup.service -p Result -p ExecMainStatus
journalctl -u hermes-paper-backup.service --since today --no-pager
```

oneshot 服务正常完成后状态为 inactive，成功与否看 Result/ExecMainStatus；定时器应为
active/waiting。工具拒绝覆盖同一天的备份；当天已有成功文件时不要直接重复启动备份服务。
手动额外备份应使用明确的新归档目录，保留已有备份和校验文件。

交接给负责主控/后续部署的 session：

- 应用库仍为 `example_paper@127.0.0.1:55432`，runtime 使用原
  `.local/private-evidence`。
- 原 controller/operator 凭据和两个摘要没有轮换；主控按原 controller.env 接入，人工运维
  使用 operator.env。备份凭据独立，不能给主控、研究模型、沙箱或交易 runtime。
- 本轮没有改主控/研究插件、模型 Profile、paper `config.yaml`、Cube 配置、部署锁或
  会话 manifest，避免覆盖另一 session 的恢复工作；相关部署状态以各 session 的最新证据为准。
- 数据库运维的本机备份/恢复这项已完成。完整预测交易仍需模型认证/调用、Cube 管理鉴权与完整
  隔离验收、最终部署锁、唯一 runtime 服务，以及真实模型模拟闭环；本轮没有启动它们。
- 本轮是同机备份，异地副本和存储故障后的整机恢复没有配置；备份失败通过 systemd/journal
  可见，与 TG 告警的衔接交给主控/通知部署阶段，本轮不向外发送消息。

## 回滚与保留

停止本轮自动备份：

```sh
systemctl disable --now hermes-paper-backup.timer
```

若服务正执行，应先确认当前备份完成或单独停止该 oneshot 服务。保留所有 dump、校验文件、
凭据、数据库和恢复副本；不重建或清空源库，不影响原行情服务与模拟数据库服务。
如需撤销新备份账号访问，可由本机数据库管理员停用该角色，并逐项核对撤销新增只读授权和
HBA 行，保留其余后来配置；不能用本轮 HBA 前副本覆盖后来新增的其他规则。

源码回滚仅恢复 `storage/backups.py` 本次事务边界修补及测试中的对应扩展，参照本轮
`before/`，保留该测试原先已完成的结构预测迁移和另一 session 的改动。
若回滚备份工具版本，应同步重建专用工具环境，不改业务安装。
未 commit/push；Feature 只追加本轮工作，changelog 不提前归档，密钥和运行状态保持 Git 忽略。
