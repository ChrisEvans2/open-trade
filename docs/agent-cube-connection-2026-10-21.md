> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Agent 与 CubeSandbox 接入、运行续期交付记录

日期：2026-10-21（Asia/Shanghai）。唯一代码仓库：`/srv/open-trade`。

后续：2026-10-23 已完成独立管理 key、专用模板的真实完整隔离和预检修复，见
[新交付](cube-isolation-and-preflight-2026-10-23.md)。本文件的未鉴权及超时状态保留为组件接入当时的历史记录。

## 交付范围与状态

本次用户要求接通 Agent 与已安装的 CubeSandbox，并在执行过程中更新已有沙箱的超时。
实现沿用每个任务 attempt 一个沙箱：runtime 创建并准备沙箱，Hermes 调度进程在宿主机运行，
终端命令通过原生 `trading-cube` 插件在沙箱内执行。没有把整个 Agent 进程迁入虚拟机。

真实 Cube / Hermes 终端组件验收已通过：创建实际虚拟机、准备只读输入与普通用户工作目录、
对同一 ID 更新超时、比较真实 `endAt`、超过原始到期时间后执行原生终端、取回 scratch 文件、
阻止从输入目录取文件，最后按归属删除并确认 404。此验收没有调用模型。

**组件连通不代表完整预测交易 runtime 已上线。** 本机 Cube 的无凭据 `/sandboxes` 请求返回
200，当前进程和部署文件未配置 `CUBE_API_KEY` / 鉴权回调；正式 runtime 的凭据及完整隔离报告
门禁保持原样。本次没有生成伪造验收报告、置 `isolation_verified=true`、迁移模型认证、启动
预测/复盘/交易服务或修改凭据。其他正式部署前置条件仍应逐项通过原有检查。

## 生命周期与续期规则

对应文件：

- `/srv/open-trade/trading-agent/src/trading_agent/runtime/agent_runner.py`
- `/srv/open-trade/trading-agent/src/trading_agent/runtime/sandboxes.py`
- `/srv/open-trade/trading-agent/src/trading_agent/config.py`
- `/srv/open-trade/trading-agent/src/trading_agent/runtime/logging.py`
- `/srv/open-trade/trading-agent/config.yaml`

1. 任务领取和输入冻结沿用已有流程。创建请求绑定 project、job_id、attempt_id、fence，
   初始超时默认 180 秒，`onTimeout=kill`、`autoResume=false`，无宿主挂载。
2. 在启动每次 Hermes 模型进程之前，runtime 先为已有沙箱更新超时；初始分析和原有格式修复
   分别使用自己的剩余进程预算，均受本 attempt 总预算约束。
3. 进程执行期间默认每 15 秒更新一次。续期前在 PostgreSQL 事务内检查授权、任务状态、
   lease、fence、runtime 归属、令牌及发布截止时间，离开事务后 GET 检查远端归属并
   POST `/sandboxes/{id}/timeout`，成功后在新事务再次 guard 并记录状态。
4. TTL = min(配置的 idle_timeout_seconds, ceil(剩余有效预算 + 15 秒回收余量))。
   有效预算同时受当前进程预算、令牌到期、任务发布截止时间和模型开始后的 120 秒总预算限制。
   不使用永不过期值，不靠续期延长任务授权、模型预算、预测期限或已发布事实。
5. 续期失败、资源消失、归属改变或任务失效时，等待器抛出异常，终止所属进程；已有 finally
   回收路径继续执行。进程退出后停止续期，续期协程取消并收束后才回收资源，避免回收后续期。
   已保存的业务事实继续保留；重试沿用已有任务规则，没有新增“静默重建沙箱继续执行”。
6. 新配置默认 `renew_interval_seconds=15`、`timeout_grace_seconds=15`。
   校验续期间隔 + 两次管理请求预算 + 回收余量 < 初始沙箱超时；非法配置拒绝加载。
   `sandbox_timeout_updated` 结构化日志记录任务标识和 `timeout_seconds`，不输出凭据或原始响应。

管理请求使用有界 HTTP 客户端，并额外用 asyncio 限制整次请求的墙钟时间（默认 8 秒），避免
仅逐阶段超时或直接使用 SDK `set_timeout()` 内部未明确限制等待时间的同步请求。
已阅读本机对应上游实现：timeout 更新会以当前时间重置空闲窗口；真实返回的
`endAt` 变化也已验证。任务截止时间仍由 runtime 独立强制执行，沙箱空闲超时不是授权期限。

## 接入时发现并修复的兼容问题

以下均是本次实际接入所需的技术配套，不代表过去已能成功连接：

| 文件/行为 | 实际问题及修正 |
| --- | --- |
| sandboxes.py 创建参数 | 本机 CubeAPI 的 `volumeMounts` 接受数组，旧 `{}` 返回 422；改为 `[]`，保持无挂载。 |
| sandboxes.py 归属核对 | POST 响应没有 metadata，GET/list 会附加系统标签。先保存返回 ID，再 GET 比对四个任务归属字段；连接文件保存完整连接响应和已验证的任务归属。回收/续期/恢复均核对这四个字段，系统标签不参与等值比较。 |
| sandboxes.py 磁盘限制 | 本机 GET 的 `diskSizeMB=0` 是未知值。仍校验 CPU/内存；磁盘未知时不直接认定通过，准备阶段始终检查 `/workspace/scratch` 实际文件系统容量，不可确认或超限即拒绝。本机实际约 1006 MiB，小于原 4096 MiB 上限。未提高资源或账户上限。 |
| sandboxes.py IPv6 | Python 模板缺少 sysctl 命令；直接向沙箱 `/proc/sys/net/ipv6/conf/{all,default}/disable_ipv6` 写 1，并沿用普通用户隔离探针。没有修改宿主网络。 |
| trading-cube 插件 | 取消时按已保存的任务归属核对远端标签；修复文件桥向 Hermes 基类传递 keyword-only `max_bytes` 的方式，真实 scratch 文件取回已通过。 |

## 本机配置

仅更新已有隔离 paper 草稿：
`.local/private-evidence` 的 sandbox 段。

- 管理地址 `http://127.0.0.1:3000`，SDK 数据通道 `127.0.0.1:80`。
- 使用已有正式模板 `tpl-a454d72f9445472cb2ac9158`（python-basic），SDK 固定 0.7.0。
- 该模板已验证存在的普通用户为 `user`，因此此草稿设 `execution_user: user`；通用代码默认
  `research` 不变。模板本身未修改。
- 超时 180 秒、续期间隔 15 秒、余量 15 秒；原资源与风控上限不变。
- `isolation_verified` 继续为 false，未伪造 isolation report 或部署锁。
- 新增沙箱配置字段沿用现有部署锁哈希机制；后续正式启用前需按最终配置重新验收并生成锁，
  不能沿用旧哈希或直接把草稿标为已验收。
- 草稿原件备份在 `.local/private-evidence`。

本机 SDK 可以通过已有回环 HTTP 端口 80 连接 CubeProxy，无需把 SDK 的 HTTP 请求发送到
HTTPS/14443。此结果不包含外部电脑到 HTTPS/14443 的链路验收；没有改端口、服务、防火墙或代理。

## 验证与限制

验证日志集中在 `.local/private-evidence`；日志不应替代正式部署报告。

- 定向 PostgreSQL 集成：`pytest-targeted.log`，15 passed（117.09 秒）。涵盖运行中重复续期、
  续期失败终止进程、任务结束后不再续期、授权收敛 TTL、I/O 后 fence 变化、失效/外来资源拒绝，
  以及原有发布/超时/格式修复/回收流程。数据库是真实 PostgreSQL16，Cube HTTP 是明确的测试替身。
- 真实 Cube 与 Hermes 原生插件：`pytest-live-cube.log`，最终复核 1 passed（28.46 秒）。
  此测试调用本机真实沙箱和真实 SDK/终端插件，不调用模型、不产生正式预测或交易。
- 单元及 Hermes 回归：`pytest-unit-hermes.log`，138 passed、2 failed；失败均为凭据预检的
  5 秒进程超时。单独复核 `pytest-credentials-recheck.log` 仍为 2 failed，没有修改原测试时限。
- 新增存储限额拒绝与管理请求总超时检查后，`pytest-sandbox-unit.log` 为 8 passed（1.64 秒）。
- 真实 PostgreSQL runtime 回归：`pytest-runtime-regression.log` 为 10 passed、3 failed；
  9 项 Cube 生命周期用例通过，另 3 项 runtime 流程在原有 8/15 秒等待窗口内未完成。
- 增加管理请求整次超时后，最终定向复核 `pytest-cube-final.log`：9 passed（43.33 秒）。
- 为排除本批代码影响，将未修改的 HEAD 只读导出到本仓库忽略目录，使用其源码/测试进行对照。
  `pytest-unchanged-baseline.log` 为 4 failed、1 passed（73.16 秒）：同样复现 3 项 runtime
  超时和 1 项凭据预检超时，另一项凭据预检在基线通过。时间表现存在差异，未把未通过回归
  说成已修复，也没有据此宣称全部测试通过。
- Ruff、格式检查与 `git diff --check` 通过；没有以测试替身证明真实模型、盈利或上线。

可复现命令（目录 `/srv/open-trade`）：

```bash
TA_TEST_DATABASE_URL='<隔离 PostgreSQL 管理连接>' trading-agent/.venv/bin/pytest \
  trading-agent/tests/integration/test_cube_lifecycle.py \
  trading-agent/tests/integration/test_agent_runner.py -q
TA_CUBE_LIVE_TEMPLATE=tpl-a454d72f9445472cb2ac9158 \
TA_HERMES_REFERENCE=.local/private-evidence \
  trading-agent/.venv/bin/pytest trading-agent/tests/live/test_cube_connection.py -q
```

新增真实组件测试默认跳过；只有显式设置 `TA_CUBE_LIVE_TEMPLATE` 和有效 Hermes 源码目录才
创建短期验收沙箱。输入明确标注 acceptance_probe，不进入预测/评分数据库；临时连接文件仅
0600 创建并在 finally 删除。测试只删除本次 ID 且归属匹配的沙箱，不删除模板或其他资源。

## 决策分类、安全与回滚

- 用户明确要求：Agent 接入沙箱；运行期间更新已有沙箱超时。
- 必要技术配套：有界续期、任务授权与归属核验、失败终止/协程收束、兼容本机 Cube API/模板、
  文件桥修复、结构化日志、配置校验、隔离草稿配置、PostgreSQL 与真实组件验收。
- 未增加独立业务功能；没有新增 Agent、修改预测/学习逻辑或调整交易风险规则。
- 本次没有调用订单接口、启用实盘、重启既有服务、修改凭据、提高全局限制或删除历史事实。
  未 commit/push；原有三个未追踪文件与既有 Feature.md 内容保留。
- 回滚代码时仅撤销本批差异；隔离草稿可从上述备份核对恢复其 sandbox 段。停止使用新续期代码
  不会改写已保存事实；本次测试沙箱应全部确认回收。临时测试 PG 结束后停止，保留历史数据与日志。

最终清理：本机 Cube health 返回 200，沙箱列表为 0（证据 `final-cube-status.json`）；
本次启动的隔离 PostgreSQL16 已正常停止，保留数据与日志。没有停止既有 Cube 或行情服务。
