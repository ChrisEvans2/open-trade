> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# TG 重复失败通知修复

日期：2026-10-24（Asia/Shanghai）。当前仓库 `/srv/open-trade`。

用户要求解决截图中 `PREDICT_PARENT / CONTEXT_EXPIRED` 一直发消息，并说明原因。

## 已核实的原因

截图任务 `<private-id>` 的第一次模型执行未保存正式结果，程序按既有规则安排重试。重试时，原冻结输入已没有足够的新鲜度余量，被前置校验以 `CONTEXT_EXPIRED` 拒绝，随后自动创建新任务刷新行情。原始任务失败和冻结输入均保留，这是既有安全校验的正常拒绝，不能通过延长期限或改写旧输入来规避。

刷屏来自两个通知问题：

1. 自动刷新路径同时发出“交易任务FAILED”通知。不同任务拥有不同通知事件键，单事件去重无法合并这些重复的恢复过程。
2. HermesSender 的投递确认预算为10秒。截图已证明用户收到至少一次消息，但该事件在数据库中仍是 `UNKNOWN / SEND_TIMEOUT`，截至本轮检查已尝试4次。旧 notifier 自动重投 UNKNOWN，所以同一事件即使已经送达，也会再次发送。事件ID只能帮助识别重复，Telegram并不依据这个文本ID去重。

首次快照为北京时间10月24日01:51：共160个 outbox 事件，SENT110、FAILED47、UNKNOWN3；3个UNKNOWN均为上下文过期通知、各尝试4次。截图任务仍为FAILED/CONTEXT_EXPIRED。不能把 UNKNOWN 当成已送达，也不能从这个统计推断用户实际收到多少条。

## 修复行为

- [任务失败路径](../trading-agent/src/trading_agent/runtime/lifecycle.py) 对 `PREDICT_PARENT`、`PREDICT_TIMEFRAME` 的 `CONTEXT_EXPIRED`、`CONTEXT_CHANGED`、`POSITION_CHANGED` 继续保存任务失败事实、创建刷新任务，但不逐次发TG失败通知。其他终止错误仍走原通知入口。
- [既有outbox](../trading-agent/src/trading_agent/notifications/outbox.py) 在领取前识别旧版本遗留、尚未结束投递的同类事件，标记 `FAILED / NOTIFICATION_SUPERSEDED` 停止投递。事件键、正文、原尝试次数和业务任务事实保留；没有删除通知或把它们伪装成送达。未过租约的SENDING不抢占。
- UNKNOWN 不再自动领取。投递超时、无法识别确认和崩溃后SENDING租约过期，均保留 UNKNOWN 等待核对，不盲目重投。明确发送失败仍按原5次上限和60/300/900/900秒退避重试；正常交易事件继续投递。
- 不新增网关、不绕开HermesSender、不改变10秒发送预算、模型预算或180秒预测上下文新鲜度。发送I/O仍在数据库事务外，通知领取和回写仍检查runtime归属及claim/lease。

UNKNOWN停止重投是本轮必要的投递取舍：无远端幂等键或可查询回执时，无法同时保证不重复和必达。本次优先解决刷屏，结果不明的事件仍可能未送达，需通过主控查询事实/本机记录核对，不自动补发。未实现Telegram的恰好一次投递，也未声称模型失败、新鲜度拒绝或所有发送超时都已消失。

## 验证和线上应用

程序回归使用隔离真实PostgreSQL 16（55432），业务库仍为58918。覆盖远端已接收但回执超时、重启不重投、过租约旧claim失效、明确失败5次封顶、旧通知停止投递、6种父/子预测自动刷新、真实错误通知保留、正常交易通知和业务事务不受发送失败影响；另回归任务授权/lease/fence、唯一runtime和paper启动守卫。

相关测试 **27 passed / 162.40秒**（20个真实PG集成、7个单元），Ruff、格式、`git diff --check`通过。首次执行因误用测试实例不存在的nobody数据库角色，19个PG用例在fixture连接阶段报错；改用该隔离实例既有`ta_test_admin`后完整重跑通过，未改用业务库、创建凭据或放宽断言。

复跑命令（在仓库根目录，先启动独立测试PG）：

```bash
TA_TEST_DATABASE_URL=postgresql+psycopg://ta_test_admin@127.0.0.1:55432/postgres \
  trading-agent/.venv/bin/pytest -q \
  trading-agent/tests/integration/test_outbox.py \
  trading-agent/tests/integration/test_structural_runtime.py \
  trading-agent/tests/integration/test_jobs.py \
  trading-agent/tests/unit/test_processes.py \
  trading-agent/tests/unit/test_paper_service_guard.py --tb=short
```

应用前paper-only启动守卫、实际文件及完整DeploymentCheck均通过；实际模型/持仓状态快照已隐去；仅重启`hermes-paper-runtime.service`加载源码修复，无需改配置或部署锁。北京时间10月24日01:54检查结果：

- `/readyz` HTTP200，所有健康检查通过，唯一runtime active。
- 3条旧UNKNOWN通知均由原notifier标记`FAILED / NOTIFICATION_SUPERSEDED`，各自尝试次数仍为4；总通知数160不变，SENT110不变，FAILED50，待发送/重试/UNKNOWN/SENDING均为0。
- 截图任务仍保留FAILED/CONTEXT_EXPIRED；历史业务记录计数与改前一致（数值不公开），未以修复通知为由修改业务事实。
- 19个凭据/风险保护文件的摘要和权限均未变。主控Gateway PID（私有进程标识已隐去）保持不变，两个Hermes Profile和Gateway服务没有重启；新模拟runtime PID（私有进程标识已隐去）。
- 58917临时测试PG已停止，58918业务PG保持运行。

北京时间01:55:47再次检查：3条停止投递的事件、状态和尝试次数与应用后相同，待投递仍为0；没有为观察过程发送新测试消息。本机证据为`before.json`、`preflight.json`、`after.json`、`observation.json`和`validation.json`。

测试里的投递器与模型为明确fixture，不冒充真实TG新消息或新模型验收；本轮不手发测试消息。线上验收证明旧队列停止投递及服务恢复，不声称已经重新验证远端超时事件的实际送达情况。

## 决定、安全边界和回滚

用户明确要求的是解决重复消息并解释；区分自动刷新通知、终止不明结果重投及旧队列停止发送是必要配套，没有新增业务功能。当前用户要求针对已运行故障，沿用已部署唯一paper runtime应用修复；不启用实盘、下单、改凭据、改风险、改预测/评分，Gateway和两个Hermes Profile保持原接入。

原有未提交工作全部保留，`Feature.md`持续忽略，本轮不commit/push。改前源码和最小只读快照位于 `.local/private-evidence`；本机目录不加入Git。

回滚只恢复本轮outbox及lifecycle增量，检查paper-only守卫和部署锁后重启同一模拟服务，不另起runtime、不回滚数据库/凭据/风险/Gateway。`NOTIFICATION_SUPERSEDED`历史事件保持终止，避免恢复旧通知风暴；不批量把UNKNOWN改为PENDING或SENT。恢复旧UNKNOWN自动重投行为会重新引入重复风险。
