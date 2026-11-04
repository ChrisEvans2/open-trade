> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 2026-10-16 模拟交易启动记录

本次用户要求启动服务、进行模拟交易，并把数据集中保存，随后补充旧 Hermes 配置在 `/home/operator/migration-stage`、Cube 尚未安装。实际开发仓库为 `/srv/open-trade`，没有恢复或启动暂存的旧实盘项目。

## 当前结果与限制

**公共行情采集服务已启动；完整预测交易 runtime 尚未启动，模型预测与模拟成交均为 0。** 不将采集运行、程序替身测试或数据库测试称为真实模型模拟交易。

- 会话目录：`.local/private-evidence`。
- 采集服务：`open-trade-paper-market.service`，每 3600 秒采集一次，目前仅启动，未设开机自启。
- 已实测 BTC、ETH 的 1h/4h/6h/12h，单份快照 8 个窗口、640 根已收盘 K 线、296 项指标字段。重复快照有重叠，不能把跨快照根数相加当作独立样本数量。
- 采集使用原有公共 GET 白名单客户端和共享量化实现，保存结构候选及各指标质量。资金费率等可选外部观测未在此准备入口采集，其相关指标保持 MISSING，不伪造数值。
- 正式 runtime 的配置完整性检查失败；启动尝试退出码 3。Cube 配置预检查返回 `CUBE_CONFIGURATION_INVALID`。实际首次启动也缺少身份摘要；第二次仅以进程内临时摘要排除该项，未持久化角色令牌或摘要，没有绕过部署校验。
- 现有部署门禁继续要求独立固定版本 Hermes、Profile、控制/研究身份、应用库权限、部署锁、真实 Cube 模板和隔离证据。没有用 fixture、跳过检查或本地终端代替 Cube。

## 目录及事实归属

| 路径（相对会话目录） | 用途 |
| --- | --- |
| `manifest.json` | 本次会话模式、假设资金、阻塞项和边界 |
| `config.yaml` | 新预测 runtime 的隔离配置草案，尚不能直接启动 |
| `config/` | 独立的 paper 执行、风险和策略配置；原仓库配置不变 |
| `market/*.json` | 每次完整公共行情、指标与结构候选，使用新文件追加保存 |
| `collector-events.jsonl` | 采集结果或错误码，追加记录 |
| `collector-status.json` | 可替换的当前状态投影；不是不可变历史事实 |
| `runtime/logs/` | 正式 runtime 启动尝试日志 |
| `validation/` | 配置预检、启动与测试证据 |
| `exports/`、`state/` | 为后续模拟导出预留；空目录不代表已成交 |

这些行情文件明确标记 `PAPER_DEPLOYMENT_PREFLIGHT_ONLY`，是准备阶段证据，不是正式预测或交易账本。正式预测、验证、交易、执行状态与知识版本仍须由原 runtime 写入 PostgreSQL；未来文件导出是 PG 的派生视图，不用文件绕过授权、lease/fence/deadline 或形成第二个可修改事实源。

## 配置决策

1. 使用仓库锁定依赖安装 Python 3.11.17；宿主 Python 3.13 不满足新系统版本约束。没有改锁文件。
2. 独立复制当前仓库的非敏感配置，将 app 和 runtime_state 的 execution_mode 均设为 paper，禁用旧 Advisor 干预及 Telegram 报告；保留风险上限和策略参数。原配置仍为原值，未复制旧账户余额、持仓、订单或任何交易凭证。
3. 虚拟初始权益沿用 `AccountState` 的默认 100 USDT，仅为假设资金。已实际调用 `load_execution` 核对 Transport 为 `PaperTransport`；不是 OKX 交易所模拟盘。
4. 当前 PaperTransport 按冻结请求价格立即模拟成交，不加入滑点或手续费模型，不能据此宣称真实交易收益。当前没有模型输出，也没有发生这类成交。
5. 旧 Hermes 非敏感配置显示 provider 为 `openai-codex`、默认模型 `gpt-5.6-luna`，另有 `trade-review` Profile。这里只读核对，未更改认证文件、启动旧网关、Cron 或 Telegram 轮询。迁移记录中的 Hermes 提交与新 runtime 要求的固定提交不同，不能直接当作已验收安装。另在 `.local/private-evidence` 检出新 runtime 指定的 `<private-artifact-hash>`，按其锁文件安装核心依赖；该版本的实际运行依赖使用独立 Python 3.14，交易程序仍为 Python 3.11。未修改 Hermes 源码，未复制认证文件或声称真实模型已可用；Profile、凭据范围和部署锁仍需在 Cube 确定后完成。
6. 新增 `scripts/collect_paper_market.py` 作为等待部署时的公共行情准备入口；不调用模型、私有交易接口或订单 transport。一次完整采集成功后才发布快照，文件不覆盖；单会话使用进程文件锁，重复启动拒绝。
7. 采集使用新的 systemd unit，设置 CPU/内存约束、只读系统和仅会话目录可写；未启用任何既有交易服务。采集频率为准备阶段默认值，不改变正式预测的独立周期和分钟验证调度。

## Cube 待部署项

官方安装预检脚本要求 `/data/cubelet` 位于 XFS；当前现有文件系统是 ext4。源码证据暂存于 `.local/private-evidence`，检查段返回错误后终止。参考[官方安装脚本](https://github.com/TencentCloud/CubeSandbox/blob/master/deploy/one-click/online-install.sh)与[官方部署说明](https://docs.cubesandbox.com/guide/bare-metal-deploy)。仅安装了 Python SDK 0.7.0 不等于已经有 Cube 服务。

本机部署的可审阅方案：新增独立 XFS 存储（独立磁盘或单独文件镜像挂载，不格式化现有 ext4 分区），安装 Docker 和 Cube 服务，限制管理接口和数据面，准备无凭证、普通用户、只读输入模板，完成仓库要求的真实隔离检查并锁定报告。需要评估 DNS/网络改动、资源占用与 SDK 0.7.0 兼容性。也可提供已有远程 Cube 的受保护地址、模板与访问配置。当前未执行磁盘格式化、镜像挂载、Docker 安装或 DNS/防火墙修改。

## 运维与回滚

```bash
systemctl status open-trade-paper-market.service
systemctl stop open-trade-paper-market.service
systemctl start open-trade-paper-market.service
```

停止只影响本次公共行情采集。保留数据目录、追加事件和启动失败证据。若不再需要服务，停止后移除本次新增 unit 并 daemon-reload；不删历史行情。未 commit/push，不改旧服务、实盘开关、凭证或风险上限。

## 验证

公共 OKX 时间接口和八窗口真实采集已成功；文件检查为每份 640 根、296 项指标；重复进程启动已被会话锁拒绝；原执行适配器加载的实际 Transport 为 paper。采集发布使用临时文件写完并 fsync 后以硬链接原子发布，已有目标不覆盖；故障验证拒绝最后一组少根，且不发布半批快照。测试临时文件明确位于 `validation/partial-batch-fixture/`，不混入真实行情目录。

- Ruff check、format --check、py_compile、git diff --check 均通过。
- 首轮真实 PostgreSQL 17 全量单元及集成回归：**232 passed、3 failed，888.48 秒**，证据 `validation/pytest.log`。本机发行版默认提供 17，与项目要求的 16 不同；失败包含缺少 16 备份工具，以及旧/新 runtime fixture 的 8/15 秒等待超时。没有把这轮写成全量通过。
- 随后从 PostgreSQL 官方 Debian 包仓库下载、按发布 SHA256 校验并本地解包 16.13，使用独立端口 55432；安装系统 libpq5，使严格清理环境变量后的备份子进程仍能解析共享库。没有更改业务代码来绕过版本检查。
- PostgreSQL 16 的行情、结构、PaperTransport、预测执行、结构 runtime、备份定向回归初轮被 SIGTERM 中断，不计完整结果；独立测试服务重跑为 **35 passed、2 failed，124.20 秒**，证据 `validation/pytest-postgres16-retry.log`。失败为结构 runtime 超时、备份子进程缺 libpq5；补齐系统库后再单独验证，**备份/恢复通过，结构 runtime 仍超时**，证据 `validation/pytest-postgres16-isolated.log`。
- 原等待预算不变的诊断重跑：**旧 runtime fixture 通过；新结构 runtime fixture 仍超过 15 秒**，证据 `validation/pytest-runtime-diagnostic.log`。测试诊断记录了任务进度、协程调用点和锁等待，不记录 token 或 payload；观察到预测任务在推进，但同时存在控制行锁等待，尚不能宣称该环境的标准闭环时限已验收。
- 为区分进度慢与永久停滞，额外使用**仅限诊断的 pytest 插件**把这一用例的等待预算临时改为 90 秒，其余断言保留；结果 **1 passed，51.69 秒**，证据 `validation/pytest-structural-extended-budget-diagnostic.log`。没有修改仓库测试文件、生产代码、任务 deadline、租约或模型预算；此结果不算原 15 秒用例通过，也不是实际模型前向验收。仍需在实际部署下评估启动耗时、调度竞争与运行资源。
- 真实公共行情采集、测试替身预测/成交、真实模型及线上验收分别报告。本次真实模型未调用、模拟成交未发生；没有准确率或收益结论。

PostgreSQL 17 和 16 的临时测试实例在验证后均停止；两个本地开发数据目录与证据保留，不作为已部署的正式模拟账本。正式应用库、受限角色、备份/导出及运行身份仍须随 Cube 和部署配置一并完成。

## 2026-10-17 后续交接

用户确认 Cube 使用本机，随后决定自行尝试部署。已停止开发 Agent 的后续部署操作；安装包、工具和独立 XFS 镜像准备情况、已停止的服务及人工操作起点见[自行部署交接](cube-self-deploy-2026-10-17.md)。行情采集继续运行，预测、复盘和模拟交易仍未启动。
