> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 新预测工作流的模拟交易启动核对

核对时间：2026-10-27 22:53（Asia/Shanghai）。仓库：`/srv/open-trade`。

后续更新（同日23:21）：用户随后授权升级数据库及运行参数，这两项已经完成，见[升级交付](paper-database-upgrade-2026-10-27.md)。业务库当前为0010，四项参数及新增表权限已核验；插件/部署锁和真实模型闭环仍待完成，交易服务保持停止。下文保留22:53核对快照，避免改写当时证据。

后续更新（2026-10-28 17:36）：部署产物已同步，研究/Cube插件、六份实际提示词及新部署锁的一致性检查通过，见[同步交付与技术说明](deployment-artifact-sync-2026-10-28.md)。交易服务保持停止，真实模型与模拟闭环验收仍待执行。

依据用户指定的[技术路线](prediction-workflow-technical-route.md)、[讨论稿](prediction-workflow-discussion.md)，并核对当前源码、实际部署文件、systemd 和业务 PostgreSQL。截图作为较早阶段的进度参考；当前部署状态以本次只读检查为准。

**新流程尚不能直接启动：业务库仍为 0009，现用配置不符合新代码要求，研究/Cube 插件与部署锁也未更新。已有账号、凭据、两个 Hermes Profile、通知目标和 Cube 隔离报告可以沿用；随后仍需完成新流程的真实模型及模拟闭环验收。**

本次只核查并记录，没有迁移业务库、修改部署配置或凭据、启动交易服务、调用模型或发送 TG 消息。

## 1. 实际使用哪套部署

规划部署目录是 `/srv/open-trade-deployment`，但当前安装的唯一 `hermes-paper-runtime.service` 实际使用：

| 内容 | 实际路径/状态 |
| --- | --- |
| 业务入口 | `/srv/open-trade/trading-agent/.venv/bin/ta` |
| 运行配置 | `.local/private-evidence` |
| Hermes home | `.local/private-evidence` |
| 部署锁 | `.local/private-evidence` |
| 业务服务 | 已安装、enabled，但当前 inactive/dead；数据库单实例锁无人持有 |
| 应用库 | 现有模拟业务库，迁移版本 `0009_prediction_knowledge` |

后续应升级这套已有入口。若需要搬迁到 `/srv/open-trade-deployment`，应作为单独的迁移工作核对全部路径和锁，不能再启动第二套交易入口。仓库内的 `trading-agent/config.yaml` 是通用示例，不是当前服务读取的配置文件。

## 2. 启动前必须调整的配置和部署

### 2.1 数据库迁移与应用权限

业务库没有 `analysis_scopes`、`analysis_rounds`、`analysis_runs`、`analysis_events`、`model_slots` 五张新表。当前代码要求 `0010_prediction_workflows`，实际调用只读的版本门禁返回 `MIGRATION_REQUIRED`。

需要在确认唯一 runtime 停止后，验证备份，再用隔离的人工迁移环境执行现有 `ta migrate`，随后运行原有 `provision/verify` 流程，为受限应用角色授予新表所需权限，并验证事实表禁止修改。备份角色对新增对象的读取权限也要复核。

已有应用、迁移、controller/operator 身份可沿用，本次确认两个原始令牌分别匹配 runtime 内的摘要，凭据文件权限均为 0600，runtime 没有混入人工、主控原始令牌或备份连接。无需重建数据库或重新生成凭据。

### 2.2 更新实际运行配置中的四个旧值

| 配置项 | 当前值 | 新流程要求 | 本次核查 |
| --- | --- | --- | --- |
| `runtime.parent_agent_concurrency` | 1 | 2 | 现值被配置校验拒绝；两名额供预测/复盘共享 |
| `runtime.agent_timeout_seconds` | 120 | 150 | 现值被配置校验拒绝；父/子各自的阶段上界 |
| `runtime.initial_agent_budget_seconds` | 90 | 120 | 现值被配置校验拒绝；父综合/各子实际分析预算 |
| `prediction.context_max_age_seconds` | 180 | 210 | 180仍可解析，但不符合本轮已确认规则，须同步更新 |

将以下片段合并到现用配置，保留其他配置和风险值；不要整份覆盖：

```yaml
runtime:
  parent_agent_concurrency: 2
  agent_timeout_seconds: 150
  initial_agent_budget_seconds: 120
  analysis_round_seconds: 1200
  analysis_window_seconds: 30
prediction:
  context_max_age_seconds: 210
```

整轮 1200 秒和固定窗口 30 秒在当前代码已有默认值，可显式写入便于运维核对。正常首次加两次重试、5/15 秒退避，以及 error 后最多三次、60/180/600 秒退避由程序规则管理，不必另造配置键。

本次仅在内存中替换四个旧值，确认新配置可解析。未将候选配置写回部署目录。

### 2.3 安装匹配的插件、提示词并重建部署锁

按内存候选配置计算，现有锁与当前源码的差异为 `engine_hash`、`prompt_hashes`、`plugin_hashes`。已安装的 `trading-research` 和 `trading-cube` 都与新源码不同；`trading-bridge` 与当前源码一致。

需要从当前代码和最终配置向新目录生成可审阅部署包，安装匹配的研究/Cube 插件，并核验新提示词、Profile、稳定偏好、引擎和部署锁的一致性。不要直接修改锁中的 hash 来跳过文件核验，也不要改变固定 Hermes 源码。

当前预测 Profile 的配置与新代码生成的规范一致，主控 bridge 也已绑定现有 runtime 地址。因此不需要重新创建两个 Profile；更新时只同步实际发生变化的产物。Profile 内传统委派参数不能替代公共 `subagent_lifecycle` 的角色预算和重试验收，具体边界见技术路线第 8.1 节。

本机持仓及未决意图数量已隐去；切换引擎前须重新检查这些前提。实施升级时必须重新检查，不能把本次快照当作永久授权，也不能为切换版本删除持仓或历史事实。

## 3. 已有配置可以继续使用

| 项目 | 本次核查 | 仍需验证的边界 |
| --- | --- | --- |
| 数据库与内部身份 | PostgreSQL active；应用只读连接成功；两个角色令牌摘要匹配 | 0010迁移后核验新增表权限 |
| 独立预测 Hermes | `prediction` Profile 已存在；`openai-codex / gpt-6-luna / high` 已配置 | 本次没有刷新认证、验证远端模型连接或测量新流程耗时 |
| 主控 Hermes 与 TG | `controller` Profile 和 bridge 已配置；原 Gateway active；通知目标已填写 | 需要验收新流程查询和通知投递，不能仅看机器人能聊天 |
| Cube | 服务 active；现有报告为 live；20/20 必需项为 true；Cube配置及报告摘要匹配现用锁 | 新插件安装后核验部署一致性，并进行真实双沙箱/八子负载及取消验收；改变隔离配置/模板时须重验报告 |
| 本机备份 | timer enabled/active；10月27日备份任务成功，最新 dump 的 SHA256 与校验文件一致 | 本次未恢复该备份；升级前仍需确认可恢复，升级后补验0010非空事实恢复 |
| 只读行情采集 | active，最近状态无错误；此服务每小时采集准备材料 | 不等于正式分钟行情持续入库；新runtime须获取新行情并补齐验证缺口 |

截图里的“主控未配置”“完整模拟从未跑通”已被后续工作推进。[旧流程实际模拟部署](paper-runtime-deployment-2026-10-23.md)及[后续预测修复](prediction-retry-fix-2026-10-25.md)已有真实模型、预测和模拟执行证据；本次库内历史记录的逐项计数不公开。它们属于旧流程历史，不能替代新工作流验收，也不能据此推导预测准确率或净收益。

当前正式分钟数据最新时间为北京时间 10月25日 09:08，最近预测发布约为当日 09:04；这是 runtime 停止前的历史事实。采集准备材料的进程仍运行，不会自行把这些正式记录刷新成当前数据。启动后须重新取行情和核验，不能直接沿用过期预测。

## 4. 模拟执行开关已经配置正确

当前 `prediction.execution_enabled=true`，隔离风险目录的 `runtime_state.yaml` 为 `execution_mode=paper`。本次先确认 paper 状态，再用内存候选配置读取执行设置，得到 `PaperTransport`，未调用任何执行方法或订单接口。

`paper.enabled=false`、`experiment.enabled=false` 对应退役入口，当前保持关闭是正确的。无需为了“启动模拟”把这两个值改成 true。沿用现有 paper-only 启动守卫、原风险配置和账户限制。

## 5. 启动后的验收，以及持续运行前要补齐的运维

配置和部署一致后，可启动唯一 paper 服务做有界验收；验收完成再转入持续模拟。需要分别确认：

1. `config check`、paper-only 守卫和部署文件检查通过；启动后带 controller 身份查询 `readyz`，确认迁移、实例锁、权限、Cube、模型与各运行循环健康。
2. BTC、ETH 各完成真实模型新轮次：所需子周期并行、有效旧预测可复用、父综合只发布一次、身份与证据引用合法。
3. 两个真实沙箱中的最多八子负载、父/子120/150秒预算、210秒输入年龄、取消和资源回收得到实测证据。失败按有限重试和error恢复结束，不通过放宽期限或风险来通过验收。
4. 正式父结论经确定性风控进入 PaperTransport，随后完成验证评分和预测复盘。允许真实模型选择观望或被原风控拒绝；不能为了凑成交伪造信号或降低风控。
5. 主控查询和既有 outbox/HermesSender 的新事件投递可核对。当前 outbox 为 SENT136、FAILED50、UNKNOWN4；历史不明事件保留，不批量改成 PENDING 或声称已送达。

以下是持续无人值守运行的补齐项，不应混同为上述配置已完成或真实负载已通过：

| 项目 | 当前缺口 | 后续处理 |
| --- | --- | --- |
| 独立 doctor 告警 | systemd 未安装 `trading-agent-doctor.service/.timer`；本次在系统Cron也未找到doctor入口，未覆盖其他外部调度 | 按实际路径和独立运行身份适配，验收失联及恢复告警，不依赖业务库存活 |
| 开机恢复 | 行情采集和现有 Gateway 的 unit 均为 disabled，虽当前 active | 核对既有Gateway启动管理方式及依赖；若需开机自动恢复，补齐对应启动配置，避免重复启动Gateway |
| 备份故障通知 | 现有备份unit未配置 `OnFailure`，本次未发现备份失败到TG的部署证据 | 配置并验收复用现有通知链的失败告警 |
| 异地备份 | 现有配置为同机备份 | 选择独立存储目的地及保留/恢复策略后实施，不把本机dump视为异地副本 |

手续费、滑点、资金费、成交延迟等完整收益模拟仍属于后续开发与验收。基础流程模拟可先运行；若要比较接近实盘的净收益，必须先补齐成本和成交模型。可选合约统计时点、市场篮子、公开清算采样的缺失继续标记 MISSING/PARTIAL，不新增基础启动门槛。

## 6. 建议执行顺序

1. 保持唯一runtime停止，重新核对空仓、无未决意图和单实例锁。
2. 保存部署文件及可恢复备份；保留全部正式预测、评分、成交和审计。
3. 人工迁移至0010，配置并验证应用及备份角色权限。
4. 合并四项参数更新，生成新部署包，同步插件/提示词并重建部署锁；保留原认证、TG和风险配置。
5. 通过静态配置、paper-only和部署检查，再启动已有唯一服务做有界验收。
6. 验收真实模型、双沙箱、正式预测→风控→模拟执行→评分/复盘和通知；补齐独立监控及恢复配置，再持续模拟。

不另建runtime或网关，不启用实盘，不调用真实订单，不修改凭据或提高全局风险限制。迁移后如需修复，按技术路线使用兼容0010的版本向前恢复；不得回灌旧备份丢弃升级后事实。

## 7. 本次检查证据、决定与回滚

本机只读核查脚本和脱敏结果位于 `.local/private-evidence`，目录保持Git忽略，结果文件为 `readiness.json`。业务查询显式使用 `SET TRANSACTION READ ONLY`、5秒语句上限并回滚；数据库版本门禁仅执行版本查询。输出只含状态、配置字段、计数和布尔核对结果，不记录原始凭据、账户明细或私有交易所响应。

本次实际执行：

```bash
trading-agent/.venv/bin/python .local/private-evidence
trading-agent/.venv/bin/ta --config .local/private-evidence config check --json
git diff --check
git check-ignore Feature.md
git ls-files Feature.md
```

核查脚本最终成功；现配置的 `config check` 退出1，进一步定位为上述三个Literal参数不匹配；版本门禁返回 `MIGRATION_REQUIRED`，均如实保留为当前阻塞。仅在内存调整后的配置解析及PaperTransport加载通过，不代表现配置或服务验收通过。核查脚本首轮最终摘要遇到Decimal序列化错误，修正仅影响本机证据输出，随后完整重跑成功，无业务写入。

本次未改业务代码、配置、迁移或测试；不重跑已在[10月27日本地提交验证](local-commit-verification-2026-10-27.md)记录的单元、原生契约与隔离PG回归，不把历史测试算作本次运行。未执行备份恢复、真实模型、Cube外部负载、TG发送或订单操作。

自主决定仅为按当前部署实况区分硬阻塞、可复用配置、上线验收和运维补齐，并新增本说明及Feature记录；不增加业务功能或准入条件。回滚本次记录只需移除本说明和对应Feature条目，保留本机证据；没有业务配置、数据库或服务状态需要回退。原有技术路线改写和其他未提交文档保持原样，不修改changelog，不commit/push。
