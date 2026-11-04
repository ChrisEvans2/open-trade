> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 模拟业务库0010升级与运行参数更新

日期：2026-10-27（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户授权范围：先升级现有模拟业务库，并调整已确认的运行参数。本轮不启动交易runtime，不安装新研究/Cube插件、不重建部署锁，也不执行真实模型或模拟交易验收。

完成时间：2026-10-27 23:21（Asia/Shanghai）。**现有模拟业务库已升级到0010，新增表权限核验通过，四项运行参数已写入实际配置。交易runtime仍停止；插件和部署锁尚未更新，不代表已开始模拟交易。**

后续更新（2026-10-28 17:36）：插件和部署锁已同步并通过文件一致性核验，详见[同步交付与技术说明](deployment-artifact-sync-2026-10-28.md)。本文其余内容保留升级当时的记录；交易服务和真实模型验收仍未启动。

## 目标与配置范围

业务数据库由 `0009_prediction_knowledge` 升级到 `0010_prediction_workflows`。复用现有迁移工具、低权限应用账号和独立备份账号，不重建业务库或更换凭据。

实际配置文件为 `.local/private-evidence`，不是仓库内的通用示例。仅更新下列四个值，保留其他配置、格式、属主和权限：

| 参数 | 原值 | 更新后 |
| --- | --- | --- |
| `runtime.parent_agent_concurrency` | 1 | 2 |
| `runtime.initial_agent_budget_seconds` | 90 | 120 |
| `runtime.agent_timeout_seconds` | 120 | 150 |
| `prediction.context_max_age_seconds` | 180 | 210 |

固定30秒归并窗口和1200秒整轮期限沿用当前代码默认值。共享两个模型名额属于已确认的调度规则，不改变账户风险上限。

## 升级前保护与隔离验证

本轮再次确认唯一 `hermes-paper-runtime.service` 为 inactive，数据库单实例锁无人持有，模拟账户无持仓且无未决交易意图。原有文档未提交改动保留。

使用现有只读备份角色，在新的受限目录生成独立升级前备份，保留既有定时备份。备份校验通过，大小49,957,558字节。业务库51张原有表（含版本表）的行数及内容摘要在备份前后相同；原业务记录保持不变，逐项计数不公开。

本机受限证据目录为 `.local/private-evidence`，包含原配置、候选配置、备份、脱敏数据摘要及操作脚本，不加入Git。目录中不复制原始凭据，连接仅从既有分离env文件读取；备份本身包含私有业务事实，仅本机受限保存。

演练与回归使用新建的独立PostgreSQL16集群，位于 `.local/private-evidence`，仅监听回环58921；业务库仍为原58918实例。恢复目标为独立新空库，不覆盖已有数据库；恢复的真实历史副本保留，不作为交易运行目标。Pytest自行生成的测试数据库依原生命周期清理。

历史比对在只读事务中执行：逐表记录行数，并对每行的数据库文本表示计算摘要、排序后聚合；统一使用UTC。数据摘要用于发现本次操作的意外改动，不作为抗恶意篡改的密码学证明。首次核查触发了本机缺失的LLVM JIT库，随后一次原查询达到30秒上限；最终改为仅在检查事务内关闭JIT、只序列化一次行内容，并采用60秒单表上限。没有修改PostgreSQL全局配置、业务查询预算或模型期限。

## 本次已完成的回归

| 检查 | 结果 |
| --- | --- |
| 部署配置与paper-only守卫单元 | 15 passed，1.75秒 |
| 隔离真实PG：存储/事实不可变、受限身份、双名额、子期限、父等待预算 | 9 passed，57.92秒 |

命令在 `/srv/open-trade/trading-agent` 执行：

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment.py tests/unit/test_paper_service_guard.py --tb=short
PATH=.local/private-evidence \
TA_TEST_DATABASE_URL=postgresql+psycopg://upgrade_test_admin@127.0.0.1:55432/postgres \
  .venv/bin/python -m pytest -q \
  tests/integration/test_storage.py \
  tests/integration/test_deployment_identity.py \
  tests/integration/test_analysis_rounds.py::test_two_slots_are_atomic_and_cleanup_unknown_keeps_capacity \
  tests/integration/test_analysis_rounds.py::test_sub_execution_deadline_is_not_extended_by_late_submission \
  tests/integration/test_analysis_rounds.py::test_parent_wait_excludes_child_time_and_only_parent_retries --tb=short
```

## 执行结果与剩余步骤

隔离演练已完成：将新备份通过现有恢复工具还原到新空库，确认全部原表行数和内容摘要与源库一致，再用现有 `ta migrate` 升级恢复副本并配置隔离受限应用角色。副本迁移后仍只有版本表发生预期变化；新权限、事实保护触发器和两个名额均核验通过。

随后在业务库上执行同一迁移，并用现有 `provision_permissions.py` 为原受限应用角色授予完整当前权限。结果如下：

| 核查 | 实际结果 |
| --- | --- |
| 业务迁移版本 | `0010_prediction_workflows` |
| 新增表 | `analysis_scopes`、`analysis_rounds`、`analysis_runs`、`analysis_events`、`model_slots`均存在 |
| 应用权限 | `verify_app_permissions`通过；事实表禁止UPDATE/DELETE/TRUNCATE/TRIGGER权限的检查保留 |
| 备份权限 | 原备份角色对五张新表均有SELECT权限，无需补授或更换角色 |
| 新事实保护 | `analysis_events`禁止修改/删除与禁止TRUNCATE的两个触发器均启用 |
| 模型名额 | 0、1两个名额均为FREE，未绑定attempt |
| 历史内容 | 原51张表中，除迁移版本表外，其余50张业务表的行数和内容摘要完全一致 |
| 关键历史计数 | 预测事实616、预测验证220,453、交易意图37、执行事件61，均保持 |
| 四项配置 | 原子替换后解析通过；比较新旧YAML，确认只有上述四个字段变化，权限仍0600，属主保留 |
| 其他文件 | 凭据、风险、Profile、已安装插件和部署锁等35个保护文件摘要均未变 |
| 运行状态 | 交易runtime保持inactive；临时58921测试PG已正常停止，恢复副本和日志保留，Pytest临时库剩余0个 |

实际配置的 `ta config check --json` 返回 `valid=true`、`configuration_complete=true`、`missing=[]`；同一服务使用的paper-only守卫返回 `paper_only_guard_passed`，未触发订单或模型调用。

在仓库根目录运行的行政操作入口为：

```bash
trading-agent/.venv/bin/python .local/private-evidence prepare
trading-agent/.venv/bin/python .local/private-evidence rehearse
trading-agent/.venv/bin/python .local/private-evidence apply
trading-agent/.venv/bin/ta --config .local/private-evidence config check --json
trading-agent/.venv/bin/python trading-agent/deploy/require-paper.py --config .local/private-evidence
```

本机操作脚本将`ta migrate`和`provision_permissions.py`放在各自隔离的子进程环境执行，业务连接不出现在命令参数或报告中。`prepare/rehearse/apply`是本轮单次操作入口，不是可随意重复执行的安装器；重试前须先核对已完成回执，避免重复备份、覆盖恢复库或混淆迁移状态。

证据文件包括`prepare.json`、`backup.json`、`rehearsal.json`、`database.before.json`、`database.after.json`、`permissions.json`、`applied.json`、`final-checks.json`、`completion.json`、`unit-results.json`和`pg-regression.log`。失败尝试也保留为`failure.json`；其中记录的是升级前只读检查曾遇到的超时，最终准备、演练和应用均有独立成功回执，未将失败覆盖成通过。

新研究/Cube插件、提示词和部署锁仍需单独同步，并验收新流程真实模型、双沙箱及模拟闭环。本次实际调用部署文件检查，仍按预期返回`DEPLOYMENT_LOCK_MISMATCH`；没有改写hash绕过检查。`config check`只检查配置，不代表部署锁匹配或服务ready。

## 安全边界、决定与恢复方式

本轮的配套决定仅为：新增独立升级前备份、用真实备份做隔离迁移演练、逐表比对历史事实，并保留隔离恢复副本。迁移与授权复用项目已有入口；外部备份/恢复进程不放入业务写事务，恢复工具持有session advisory锁并在调用pg_restore前结束检查事务。

不启用实盘、不调用真实订单、不改变凭据或全局风险，不删除或改写历史预测、评分、交易和审计。现有两个Hermes Profile、Gateway、通知链和Cube服务保持原接入。临时PG在工作完成后停止，保留恢复副本与日志。

0010迁移不提供删除事实的downgrade。升级后保持runtime停止；若后续发现问题，应使用兼容0010的修复版本向前恢复。配置可以在确认仍无人运行且没有后续配置变更后，单独恢复本轮前的受限副本；不能因此声称数据库回到0009，也不能启动旧版本或回灌备份丢弃后续事实。不commit/push，不提前归档changelog。
