> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Runtime 重构与旧研究清理对后续配置、开发的影响

复核日期：2026-10-22；仓库 `/srv/open-trade`。
最新更正：按用户要求恢复主控 Hermes/Gateway/TG 与独立预测 Hermes 的桥接，
trading_job_create 已恢复并接入当前结构解释/复盘任务；本页的退役建议与随后删除记录
保留为过程证据，当前行为以[恢复交付](controller-hermes-restore-2026-10-22.md)为准。
依据：[结构收敛交付](runtime-core-consolidation-2026-10-22.md)、
[过期内容清理交付](obsolete-research-cleanup-2026-10-22.md)，并核对当前代码、本机模拟库、
实际 paper 草稿、已生成凭据和部署包。

## 结论

可以继续沿当前结构配置和开发，不需要重建数据库、重新生成内部令牌或重新填写 OKX 凭据。
这次清理涉及旧测试、旧入口和日志文档；正式数据库事实、迁移、行情快照及本机凭据保留。
部署依赖、工具入口和验证命令需要按新结构更新。发现一个尚未修复的主控工具衔接遗漏，
以及两个不能直接安装的历史/验证用部署包；详细范围如下。

本轮是兼容性评估，只新增说明与本地验证证据；没有修复业务源码、改写配置或启动交易。

同日后续：用户追问后已在仓库源码移除下述 `trading_job_create` 注册、清单与 POST 处理器；
详见[补清理说明](obsolete-research-cleanup-2026-10-22.md#同日后续补漏移除主控失效任务工具)。
以下发现保留为本次评估时的证据，不代表补清理后的源码状态。未安装 Profile 或部署新包。

## 已配置内容可以继续使用

| 项目 | 本轮实际检查 | 对后续的影响 |
| --- | --- | --- |
| 本机模拟库 | 应用账号实际只读连接成功；版本仍为 `0009_prediction_knowledge`；50 张业务表与当前 metadata 一致，32 张事实表权限检查通过 | 继续使用原库和受限账号，无需执行清库、重建或本次结构迁移 |
| 已存业务记录 | 仍只有 `runtime_control` 的 1 条初始化记录，其余业务表为空 | 没有预测、评分或成交被本轮检查改写 |
| 内部身份 | controller/operator 原始令牌与两个摘要、角色仍匹配，四份凭据文件均为 0600 | 继续使用现有分离 env 文件 |
| OKX 只读接口 | 新进程使用当前 `trading_core` 签名客户端实际 GET 时间和账户配置成功；权限仅 read_only、传输 GET_ONLY | 根 `.env` 继续用于只读行情，不需要更换密钥，无订单请求 |
| 现有 paper 草稿 | 当前 `load_config` 与 `load_execution` 成功，execution_mode=paper，传输为 PaperTransport | `legacy_config_dir` 兼容键仍有效，继续指向原风险配置目录，不用为了包移动重写风险参数 |
| 原有文件 | `.env`、paper `config.yaml`、执行 `app.yaml`、`runtime_state.yaml` 与建库时的内容摘要一致 | 重构没有改变这些文件中的配置和凭据 |
| 行情采集 | 原服务仍 active，05:04 UTC 检查的最新快照采于 04:07 UTC，8 个周期窗口、USER_API_GET_ONLY、error_code=null | 保留已有快照与采集服务；此时仍是启动准备材料，尚未写入正式行情表 |
| Agent 与沙箱 | 续期间隔仍 15 秒、idle timeout 180 秒；当前结构任务的重复续期、失败回收回归通过 | 上轮续期接入没有因拆包失效；本轮未重跑真实 Cube 隔离验收 |

上述实际应用库检查在 READ ONLY 事务中完成。本轮 PG 回归另用回环 55432 的临时测试集群，
没有把本机模拟库 55432 填入测试配置。行情与数据库原服务没有被重启。

## 发现的接入遗漏：主控仍暴露已退役任务工具

补清理前 `trading-agent/integrations/hermes/trading-bridge/__init__.py` 第 93 行仍注册
`trading_job_create`，描述为“请求解释、复盘或受配置限制的实验”；第 48 行将这些请求发送到
`POST /v1/jobs`。对应 `plugin.yaml` 也仍将工具加入清单。

当前 `trading-agent/src/trading_agent/api/app.py` 第 97–105 行已经在认证后将该写接口统一
退役为 `410 LEGACY_FLOW_RETIRED`。本轮用合成身份、当前插件处理器和实际 ASGI 中间件复现：
主控可看到并调用此工具，但最终必定收到 410。没有启动真实 runtime 或使用真实身份令牌。

这会影响主控 Profile 的后续接入：模型可能反复尝试一个无法完成的任务工具。
建议在生成正式部署包前，从主控插件、manifest 和相关说明中移除此退役写工具，保留历史 GET
和 v2 预测/意图查询。不要为消除错误重新启用旧研究入口。若以后需要人工触发当前结构复盘，
应单独定义受限的新任务入口和测试，而不是恢复旧流程。

此项在本次评估中作为**待修复发现**报告，评估未改动插件或 API。随后已补清理插件，
并扩展原生工具发现和查询契约；API 继续拒绝旧写入口。评估时 bridge 检查主要覆盖查询路由
和凭据边界，不能据其当时通过推断这个任务创建链路也有效。

## 部署产物、依赖和服务须按当前配置重新准备

1. **2026-10-21 原 paper 部署包已过时。**
   `.local/private-evidence` 的锁与当前 paper 配置
   比较，差异为 `plugin_hashes`。旧研究插件还含已移除的 `forecast_submit`。不能原样复制到
   Hermes Profile；应从当前代码生成新的插件与部署锁。
2. **2026-10-22 重构验证包也不能直接套到本机会话。**
   `.local/private-evidence` 的插件 hash 在评估时
   匹配源码，补清理主控工具后也已过时；锁中的 provider、model、controller/trading Profile、bridge_settings、Cube、
   engine/profile hash 与实际 paper 草稿不同。它证明独立包可构建，不是本机最终部署配置。
3. **以后只安装当前三个包并使用 `ta serve`。**
   runtime 依赖 `trading-core` 与 `trading-quant`；根目录是开发工作区，归档代码不进入部署。
   三组件依赖、导入、结构提示词与迁移资源的定向单元检查通过。后续机器部署仍需安装本批新
   产物，不能依靠旧环境里恰好残留的旧应用或 `trade-agent` 命令。
4. **systemd 文件仍是通用模板。**
   当前模板使用 `/opt/trading-agent`、`/etc/trading-agent/runtime.env` 和 `postgresql.service`；
   本机已配置的数据库服务实际叫 `example-paper-postgres.service`，凭据位于隔离会话目录。
   安装唯一 paper runtime 时必须适配服务依赖、路径、系统用户、配置读取及写目录权限。
   本轮没有安装此交易服务。

最终 bundle 应由当前代码和
`.local/private-evidence` 生成到**新目录**，
保留旧包作证据；模型和 Cube 最终配置确定后再冻结部署锁，并通过完整验收。
两个历史包目前均未安装，草稿指定的 Hermes home 和最终部署锁仍不存在，因此没有在线服务
因本次清理立即加载错包的情况。

## 后续开发与验证范围

- 当前主线继续是结构周期预测、父综合、知识点/案例/procedure、确定性执行和期限学习。
  开发目标没有因旧研究清理改变。历史表保留用于读取，不意味着要恢复旧调度、旧策略迭代或旧小时分类研究。
- 本机数据库配置交付中的 `51 passed` 是**清理前的历史结果**。其中原命令含已删除的
  `tests/integration/test_api.py`，不能再作为当前一键复核命令。
  当前应使用 `test_prediction_api.py` 和 `test_runtime_consolidation.py` 验证 v2 接入及历史保护，
  保留 `test_deployment_identity.py`、`test_jobs.py`、`test_storage.py`，沙箱执行使用
  `test_prediction_runner.py` / `test_cube_lifecycle.py`。
- 文档明确保留的高频调度锁竞争问题仍未解决。本轮没有运行压力测试或修复锁调度；先前以较低
  测试轮询频率通过正常启动，不等同于高频压力问题消失。后续应定位事务持锁与并发开销，并验证
  正常生产频率和恢复场景，不能用提高风险限制、延长期限或改写旧评分解决。
- 仍需完成独立模型 Profile/认证、Cube 服务端强制鉴权与完整隔离报告、部署锁、唯一服务、
  通知及备份恢复策略，最后做真实模型的完整模拟联调。这些前置项与前轮一致，不需要重新开发
  已有数据库或行情签名功能。

## 本轮验证与边界

证据目录 `.local/private-evidence`：

- `local-compatibility.json`：实际配置、只读应用库、令牌匹配、原文件摘要、原采集状态与部署包检查。
- `bundle-differences.json`：两个保留部署包与当前 paper 配置的差异字段。
- `fresh-readonly-api.json`：当前新核心在独立新进程中的真实 OKX GET 验证，权限仅 read_only；无私有原始响应。
- `retired-controller-tool.json`：合成身份复现主控工具到实际退役 API 的 410，不是在线运行证据。
- `unit-compatibility.log`：47 passed，9.66 秒。
- `postgres-compatibility.log`：4 passed，33.00 秒；实际受限角色部署检查、两个结构任务续期/清理
  场景、旧写入口退役及历史保留。使用明确模型/沙箱替身和真实独立 PG，不作为真实模型/Cube 验收。

复核命令，工作目录 `/srv/open-trade/trading-agent`：

```sh
.venv/bin/pytest -q tests/unit/test_runtime_packaging.py tests/unit/test_authenticated_market.py tests/unit/test_operations.py tests/unit/test_sandboxes.py tests/unit/test_deployment.py
TA_TEST_DATABASE_URL=<独立测试连接> .venv/bin/pytest -q tests/integration/test_deployment_identity.py tests/integration/test_prediction_runner.py::test_renewal_failure_stops_running_agent_and_reclaims_sandbox tests/integration/test_prediction_runner.py::test_running_agent_renews_repeatedly_and_stops_renewing_after_cleanup tests/integration/test_runtime_consolidation.py::test_old_writers_retire_before_parsing_and_history_stays_readable
```

临时测试 PG 已正常停止；持久模拟数据库和原只读行情采集继续运行。没有模型前向、模拟成交、
订单操作或收益结论。没有改动凭据、风险、历史事实、生产配置或主机交易服务；所有此前未提交
工作保留，未 commit/push。回滚本次评估仅需移除这份新增说明和对应 Feature 条目，无业务配置
或数据库迁移需要回滚；本地验证证据可保留。
