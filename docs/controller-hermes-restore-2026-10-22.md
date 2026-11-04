> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 恢复主控 Hermes、Gateway 与独立预测 Hermes 的业务桥接

日期：2026-10-22；仓库 `/srv/open-trade`。用户纠正了前次清理的角色边界：
主控 Hermes 是 TG 交互入口，预测父 Agent 是独立预测 Hermes 中的交易分析角色。
此前退役 `/v1/jobs`、随后移除 `trading_job_create`，误删了主控申请任务的能力。
本次按用户要求恢复这条能力，并接入当前结构预测 runtime。

## 保留的系统分工

**你 / TG ⇄ Hermes Gateway ⇄ 主控 Hermes ⇄ 交易 runtime ⇄ 独立预测 Hermes**

| 组件 | 职责 |
| --- | --- |
| 主控 Hermes | 与用户对话，查询预测、持仓、运行状态、任务与复盘结果，申请受限研究任务 |
| 既有 Hermes Gateway | TG 消息收发、会话与路由；不另造网关 |
| 独立预测 Hermes | 分周期子 Agent、预测父 Agent 综合及预测复盘；输出预测、交易意图或解释 |
| 唯一交易 runtime | 调度、沙箱与租约、事实保存、确定性风控和执行、通知队列 |
| trading-core / trading-quant | 共享交易核心与共享量化计算；不增加独立调度器 |

既有 HermesSender、通知队列和独立 Profile 配置没有被前次删除。本次恢复主控请求桥接，
不恢复归档旧调度器、旧小时分类研究程序、旧直接 TG 交易机器人或策略改代码工具。
交易父 Agent 与用户交互主控不可混称；README 和两级 AGENTS 已明确该边界。

## 恢复后的接口与流程

- `trading_job_create` 重新注册并列入插件 manifest，通过 `POST /v2/jobs` 申请任务；
  `POST /v1/jobs` 兼容旧主控，两个路径共享幂等记录。二者都创建当前 `PREDICT_REVIEW`，
  不把旧 `EXPLAIN/REVIEW_DAILY/EXPERIMENT` 交给归档 worker。
- `kind=explain` 接受一个 `prediction_id` 或兼容的 `forecast_id`。冻结指定结构预测及其
  原始输入/已存验证；旧 forecast 也可只读解释其快照与结算。无需新行情，不补造评分。
  预测 Hermes 通过现有 review_submit 保存叙述；确定性程序拒绝解释任务的知识修改。
- `kind=review` 接受带时区的半开区间 `[range_start, range_end)`，跨度最多 90 天且不含
  未来；可选一个启用币种，省略币种则按当前 BTC/ETH 配置分别排队。
  返回 `job_ids`，并保留兼容的首项 `job_id`。冻结输入与保存复盘都保留真实请求区间。
  当前四周期复盘行情明确是复盘时点的背景，不伪装为原预测已知证据。
- `kind=experiment` 保留入口与原配置/规格门禁。关闭时明确返回 EXPERIMENT_DISABLED，
  即使手工打开配置也不跳过合约模拟验收；本轮没有启动旧收益实验。
- 主控可用 `trading_job_get` 查询状态和 result_id，再用 `trading_review_get` 解释结果。
  历史查询及 v2 预测/交易意图查询保留；补入 `trading_status` 和 `trading_positions`，
  持仓读取当前部署的数据库投影，不调用交易所订单接口。

请求仅接受 controller/operator 身份。研究任务令牌不能申请新任务，主控令牌也不能冒用
预测提交权限。请求在同一事务内检查 runtime 所有权、暂停状态和幂等键，创建任务与追加
审计；任务执行、冻结与提交沿用授权、租约、fence 和期限检查。暂停时拒绝新任务，已受理
请求的幂等重试仍可返回原结果。外部行情、模型和发送均不在数据库事务内。

## 通知复用既有通道

手动解释/复盘的结果与通知入队在同一提交中保存。交易执行保存 FILLED、CANCELED、
REJECTED、STALE 或 UNKNOWN 事件时也在同一事务按事件键去重入队；只发状态与对象 ID，
不把原始交易所响应交给主控或 TG。最终 FAILED/SKIPPED 任务保存失败后入队通知。
通知发送失败不会撤销业务事实，仍由原 outbox 重试和 claim fence 处理。

既有 HermesSender 调用 `hermes -p <controller_profile> send --to <notification.target>`。
主控后续查询事实并解释结果，独立预测进程仍使用 trading_profile 的 `chat --oneshot`。
本次补齐当前事实到原通道的连接，没有新增 TG SDK、机器人凭据或第二套 gateway。

## 验证与实际接入状态

实际结果：

| 验证 | 结果 |
| --- | --- |
| 全 runtime 单元与固定 Hermes 原生插件三模式 | 132 passed，48.24 秒（129 单元 + 3 Hermes） |
| 最终源码的安装边界与部署单元复核 | 8 passed，10.36 秒，与上项重复，不叠加统计 |
| 当前主控请求、历史事实保护、知识复盘、通知、确定性执行的真实 PG 回归 | 分批合计 27 个不同用例通过 |
| 静态与依赖 | 当前 src/tests/integrations Ruff、11 个修改 Python 文件格式、两份锁离线校验、入口/交付链接、git diff --check 通过 |
| 执行器与 runtime 方法范围 | 对本轮前副本做 AST 对比，仅 ExecutionService._event 和 Runtime._failure 改动；风控、发单、成交与仓位处理方法未改动 |

PG 失败与修正证据完整保留：首轮 12 passed / 2 failed，154.87 秒。一项测试误期望缺少
幂等键返回 400，实际原契约为 422，修正测试；另一项为新请求的 after validator 尝试
修改不可变对象，改为字段时区校验器，保留 UTC 规范化。复测主控与执行 16 passed /
1 failed，185.92 秒；唯一失败为用“全局暂停”场景误期望 REJECTED，实际原执行器正确
记录 STALE。改用 fixture 的禁止新开仓风控条件后 1 passed，16.03 秒，未修改生产风控。
最后 17 个不同用例，加首轮中其他未受修改影响的历史/知识/通知 10 项，共 27 项通过。

实际分批命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
TA_HERMES_REFERENCE=.local/private-evidence .venv/bin/pytest -q tests/unit tests/hermes/test_plugins.py
.venv/bin/pytest -q tests/unit/test_runtime_packaging.py tests/unit/test_deployment.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/integration/test_controller_bridge.py tests/integration/test_runtime_consolidation.py tests/integration/test_prediction_knowledge.py tests/integration/test_outbox.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/integration/test_controller_bridge.py tests/integration/test_prediction_execution.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q 'tests/integration/test_controller_bridge.py::test_execution_facts_queue_one_controller_notification[True]'
```

证据位于 `.local/private-evidence`，包括修改前副本、配置缺项摘要、
方法对比和失败/成功日志。独立测试 PG 已正常停止，原模拟库与行情采集服务仍 active。
模型、市场和消息传输采用明确替身；PostgreSQL 使用独立 55432 测试集群，不连接本机
55432 模拟应用库，也不进行真实模型或 TG 发送。

只读检查当前 paper 草稿：主控 Profile 名称已配置且与预测 Profile 不同，但主控目录
尚不存在，notification.target 为空。因此本次源码恢复不能报告为真实 TG 已接通。
用户随后确认：bot 凭据及 API/会话 ID 仍在旧服务器，尚未迁移到本机；本轮保留配置空值，
不生成替代 bot 或猜测会话目标，真实通信待该资料迁移后再验收。
后续复用用户实际主控/Gateway 配置、配置 TG 目标并完成两端 Profile、模型与 Cube 验收，
再用最终配置重建 bundle/部署锁。旧 bundle 保留，不自动安装；不改凭据或绕过部署门禁。

## 自主实现决定和回滚

- 恢复旧工具名和 v1 兼容路径，内部复用当前任务，保持一个 runtime；不整批回退结构收敛。
- 解释使用原始冻结证据并强制空 changes；请求复盘限 90 天跨度，缺省按启用币种分别创建，
  明确返回多个任务，避免默默丢失 ETH 范围。
- 加入状态/持仓查询及事实驱动通知，补全用户截图所示主控职责；这些是本次补齐，
  不声称前次清理后已存在完整交易通知链。
- 保留没有 range 字段的已冻结复盘输入兼容读取，不重写旧输入、预测或评分。

未启动、重启交易服务或 Gateway，没有真实 TG 消息或订单，不修改凭据、执行开关、
风险上限或历史事实。保留此前全部未提交工作，Feature 继续忽略，未 commit/push。
回滚只按 `.local/private-evidence` 逐文件核对恢复本批改动，
删除本轮新增 API/测试/说明时保留后来改动；不批量 reset/clean，不通过清库回滚。
