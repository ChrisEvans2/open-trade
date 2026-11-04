> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 模拟 runtime 部署与闭环验收（2026-10-23）

已生成并安装最终部署锁，部署唯一模拟 runtime，完成真实模型预测→既有风控→模拟开仓/结构失效平仓→真实模型复盘。实盘始终关闭，原凭据和风险配置保持不变。以下时间均为 UTC；北京时间为次日加8小时。

## 实际验收事实

> 本节包含私有账户、部署、预测/复盘或个人知识库记录，公开版隐去原始内容；技术方法保留在其他章节。

## 已提交的真实模型复盘

> 本节包含私有账户、部署、预测/复盘或个人知识库记录，公开版隐去原始内容；技术方法保留在其他章节。

## 部署与权限决定

- 当前仓库为 `/srv/open-trade`。实际复用已有部署会话 `.local/private-evidence`，避免在 `/srv/open-trade-deployment` 再开第二交易入口。业务数据库仍为既有58918实例。
- 新增[模拟守卫](../trading-agent/deploy/require-paper.py)：先读取执行状态并拒绝非paper，再加载传输并要求PaperTransport，禁止退役paper/experiment入口。启动前live状态在构造传输前被拒绝。
- 新增[服务单元](../trading-agent/deploy/hermes-paper-runtime.service)，安装至`/etc/systemd/system/`。复用root私有解释器和Profile，没有放宽`/root`或密钥目录；使用NoNewPrivileges、只读系统/家目录和配置/风险/秘密/插件/锁，限定既有会话目录可写。主进程尚未迁移独立Unix用户，属于部署限制。
- 只恢复研究必需权限：允许原生只读tool_search/tool_describe；tool_call仍先解包再检查实际工具。CLI直接展示少量授权工具schema；子Agent轮数从3改为6，保留90/120秒预算、单模型并发、截断拒收及lease/fence。子Agent不能发布父决策/复盘或再委派。
- 预测TG对话仍为空工具集，不开放主机terminal、SQL、订单、写代码或退役策略迭代。主控Gateway和两个TG通道保留，未另造网关；通知复用outbox/HermesSender。
- 唯一runtime使用受限PG应用身份，不注入迁移/备份账号或controller/operator原始令牌。凭据及风险相关19个保护文件摘要与改前一致。
- 提示词的精确容差改动生成了新引擎版本，发生在本轮首笔开仓前；旧任务按ENGINE_CHANGED跳过并保留。后续错误提示和调度修复仅更新插件锁/程序，不切换有持仓账户的引擎。

## 实测阻塞与修复

1. 原生工具发现被研究guard误拦：恢复两个只读发现入口，并验证间接调用仍不能绕过角色边界。测试依赖补入snowballstemmer，固定Hermes源码未改。
2. 子任务三轮耗尽：直接展示工具schema并将子任务上限调为六轮，保持墙钟预算和严格报告校验；未把截断JSON当成成功。
3. 重试复用临近过期的预测输入：启动前预留120秒模型与20秒沙箱准备，余量不足即走新任务刷新。严格180秒新鲜度保留，不改写旧冻结输入。
4. 模型float重算容差导致BOUNDARY_REFERENCE：新输入冻结程序按原Decimal公式计算的fallback_tolerance，提示词要求原样引用，边界校验不放宽。
5. 模型错放/重复/抄错引用：静态纠错提示说明字段边界；预测提交失败时可从runtime只读context.json取出经UUID校验的本次input_id，提示可只引用该ID。不会自动改payload、透传私有响应或其他context字段；无文件/无效ID安全降级。
6. 刚收盘分钟K线尚未发布：缺口任务保留AWAITING_DATA，5秒后以新attempt/fence补齐，完整后才成功；持仓monitor继续运行，缺口仍阻断不完整数据交易。
7. 数据质量切换反复排父任务：整批补齐后比较子预测ID集合及持仓身份/status/revision/数量/方向，忽略单纯监测绑定和游标更新。正式提交继续校验完整context_revision；确实改变的上下文必须重综合。
8. UP目标放在触发价之下引起500：局部捕获PublishedPrediction几何校验，返回PREDICTION_GEOMETRY/422并给静态提示。错误报告不入库，不能改冻结边界适配目标。
9. 预测新鲜度检查误覆盖复盘重试：已限定为PREDICT_TIMEFRAME/PREDICT_PARENT；历史复盘可在新授权租约/fence下原样复用冻结as_of，既不重新抓行情也不混入新知识。原失败ETH任务保留，以controller新任务补跑。
10. 持续预测刷新延后人工复盘：带服务端requested_by且request_kind为review/explain的已鉴权主控任务优先取得下一模型槽；确定性执行/验证仍优先，定时复盘仍排在预测后。没有提高并发或预算。

所有外部行情、模型及通知I/O均不新增到数据库事务内。任务写入继续在同一事务检查授权、租约、fence和期限；历史预测、评分、交易、失败尝试与审计不删除、不改写。

## 验证与本机证据

本轮覆盖去重96个用例（42单元、4固定Hermes原生、50真实PG），分别在组合/定向回归中通过。collect-only仅用于核对不同节点数，不冒充执行结果。一次runtime/主控组合进程收到SIGTERM未计通过，完整重跑8项通过（88.86秒）；任务/租约/fence8项通过（57.50秒），请求调度3项通过（23.65秒）。Ruff、格式、git diff --check及systemd-analyze verify通过。

相关单元、固定Hermes原生回归与真实PG集成分别执行；测试替身不冒充线上预测或成交。重点覆盖paper启动拒绝live、工具与TG边界、间接调用、冻结精度、过期/余量、历史复盘重试、分钟缺口、持仓监测绑定、几何错误、请求优先级、幂等、fence、部分成交和唯一runtime。

可复跑的命令（真实PG命令需预先提供隔离测试库的TA_TEST_DATABASE_URL，不能指向业务库）：

```bash
TA_HERMES_REFERENCE=.local/private-evidence trading-agent/.venv/bin/pytest -q trading-agent/tests/hermes/test_plugins.py
trading-agent/.venv/bin/pytest -q trading-agent/tests/integration/test_prediction_scheduler.py trading-agent/tests/integration/test_jobs.py
trading-agent/.venv/bin/pytest -q trading-agent/tests/integration/test_prediction_pipeline.py trading-agent/tests/integration/test_prediction_market.py trading-agent/tests/integration/test_prediction_execution.py trading-agent/tests/integration/test_structural_runtime.py trading-agent/tests/integration/test_controller_bridge.py
```

本机证据在`.local/private-evidence`：改前副本、各次部署包、正式锁、预检、source_hashes、保护文件摘要、数据库快照、首次成交与最终执行事实、四份复盘和最终attestation。目录包含私有配置，仅本机保存，不加入Git。

Feature.md仍忽略；所有此前未提交改动保留，本轮没有commit/push。当前部署依赖此工作区，源码指纹记录于attestation，未声称是脱离工作区的完整打包发布。

最终运行快照（UTC19:01）：readyz200，数据库、实例锁、部署、Cube、模型、scheduler/executor/notifier/recovery和日志关键检查均通过；预测事实28、意图4、复盘4，初始知识版本仍4。服务enabled/active，19个凭据/风险保护文件摘要未变化，58917临时测试PG已停止，58918业务PG持续运行。

## 已知限制与回滚

- 基础模拟仍按冻结价格成交；完整手续费、滑点、资金费和成交延迟尚未验收，不能外推实盘收益。
- 模型可能输出错误字段、过期上下文或超时；拒绝/重试事实保留。本次证明完整闭环发生过，不代表连续无失败SLO或预测准确率已验收。
- 截至UTC19:01，outbox为SENT30、FAILED8、UNKNOWN0。8条历史通知因10秒发送预算超时失败，仍需后续运行维护处理；TG机器人能回复不能替代投递验收，未把失败或未确认通知标成已送达。

回滚先`systemctl disable --now hermes-paper-runtime.service`，确认释放实例锁并清理属于该实例的沙箱，保留全部数据库事实。已有持仓或未决意图时禁止直接换旧引擎锁，应由原版本按既有退出/恢复规则处理。配置和插件恢复时同时生成一致锁，保留后续TG/认证修改；不回滚凭据、数据库、Gateway、备份或只读行情服务。只回退本轮源码增量，保留其他未提交工作。
