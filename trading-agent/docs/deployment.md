> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

> 2026-10-22：唯一入口为结构预测 `ta serve`。安装本仓库 runtime 与 `trading-core`、`trading-quant` 两个共享包；旧交易 CLI/服务模板已归档，不安装、不启动。

# 服务器迁移准备

本文是部署包说明，不代表服务器已经迁移、Cube隔离已验证或真实模型已经跑通。
现有实盘Agent、Gateway、数据库和Hermes工作目录保持独立；迁移阶段再核对路径与用户权限。

## 进程与凭据

`deploy/trading-agent.service`仅管理一个业务runtime。部署时调整安装路径和系统用户，
配置runtime.data_dir及hermes.home为`/var/lib/trading-agent`下的目录。PostgreSQL16由系统服务管理。
Hermes本身需要固定提交的独立、无源码改动安装；同机已有源码若有改动，应保留并另建安装。

runtime.env只能包含TA_DATABASE_URL、两个角色token的SHA256摘要及Cube管理凭据。
原始TA_OPERATOR_TOKEN只存人工操作环境；TA_CONTROLLER_TOKEN只给主控插件、查询CLI和外部doctor。
模型认证仅放交易Profile受限的凭据文件。不要把人工operator凭据复制到Hermes或runtime。
`.env.example`没有可用密码；部署人员在自己的受限环境配置。

人工迁移环境使用TA_MIGRATION_DATABASE_URL运行`ta migrate`。该命令必须取得与runtime同一把
数据库advisory锁，持锁期间执行Alembic；有runtime存活则拒绝。应用角色由管理员单独创建，
再运行`deploy/provision_permissions.py --database <明确库名> --role <应用角色>`。
runtime拒绝superuser、管理角色及能修改事实表的角色，并检查所需表权限是否齐全。
备份使用专用备份凭据，不给模型工具。

2026-11-03 的分钟主动成交采集新增迁移 `0011_trade_minutes`。授权部署时需迁移并重新执行
上述权限配置；仅 `trade_raw_cache` 额外授予 DELETE，用于清理短期公开逐笔缓存，不能把
删除权限扩散到行情、预测、评分和审计事实表。默认在唯一 runtime 内按分钟采集并校验，
不由 Agent 申请工具；冷启动历史回补未完成时指标允许明确缺失，不能据此宣称全覆盖。
配置、预算、缓存及验收状态见[分钟聚合交付说明](../../docs/trade-minute-aggregation-2026-11-03.md)。

## Profile与部署锁

填写真实配置后用`deploy/prepare_bundle.py --config <路径> --output <新目录>`生成可审阅部署包。
脚本不会安装到已有Profile。将trading-research和trading-cube放交易Profile；trading-bridge只给主控。
交易Profile的模型、白名单、delegation.max_iterations=3和只读偏好文件按部署包固定。
保持Hermes原生state.db/FTS5。不要加入file、memory写入、通用delegate、MCP、Cron或消息工具。
当前工具白名单允许解释/复盘通过终端访问公网；宿主web/browser未开放。

Cube插件声明`python_dependencies: [cubesandbox==0.7.0]`；通过Hermes插件安装机制准备依赖。
正式启动核对deployment.lock.json、提示词、插件和Profile hash、Hermes提交、应用数据库权限，
以及真实隔离报告。任何一项不一致都不能通过readyz。

## Cube控制面与数据面

SDK0.7.0的命令/文件URL使用HTTP；适配器只接受127.0.0.1代理节点，远程访问须先提供
受保护隧道，本机Cube节点可直接使用127.0.0.1。远程管理入口必须HTTPS或本机隧道。
服务端必须启用认证并限制管理端口；设置客户端API key不能证明服务端已经强制认证。
模板不得预置allowOut/L7例外、宿主卷、sudo权限或可被普通用户绕过的guest管理接口。

runtime先记录创建意图，再创建沙箱；metadata含project/job/attempt/fence。复制本任务白名单
输入到root持有的/workspace/input，普通用户只写scratch；禁用IPv6。正式预测deny-all出站；
解释/复盘允许公网但拒绝私网、元数据及配置的敏感公网IP。普通子任务cleanup只关客户端，
runtime负责确认最终销毁；失败保留待清理记录，重启按数据库归属回收。

完整隔离验收须在真实Cube执行。报告kind必须为live，记录template、sdk_version及
isolation_config_hash（部署锁cube配置的canonical hash），逐项checks来自
runtime/deployment.py的REQUIRED_ISOLATION_CHECKS，保留命令及原始证据附件。
将报告SHA256记入部署锁，同时配置sandbox.isolation_report和isolation_verified。
不可把本地fixture报告改名当成真实验收。本机已生成20项检查通过的真实报告并修补guest管理隔离，
见[Cube与预检交付](../../docs/cube-isolation-and-preflight-2026-10-23.md)；主控、最终部署锁与业务runtime仍未部署。

## 运维与后续验收

readyz需controller身份；healthz仅报告进程存活。循环退出、实例锁丢失、数据库不通、
部署锁变化或Cube探针失败使readyz失败。日志异步排队、按大小轮转，剔除payload、URL和凭据。
doctor失联报警不依赖业务数据库，相同错误24小时最多一次，恢复时一次。

部署后仍需实际通知连通性、普通用户与网络隔离、模型预算、崩溃恢复和备份恢复验收。
结构预测、知识复盘与原风控的本地实现见 [当前实现](../../docs/prediction-driven-implementation.md)。
PaperTransport 只验证基础成交链，不模拟完整费用和滑点；真实模型效果及收益必须独立验证。
旧合约实验和 CSV 导入门禁保留，不是当前模拟启动入口。本机部署进展见
[只读数据与后续工作](../../docs/readonly-data-and-next-steps-2026-10-22.md)。

## 实际备份与空库恢复工具

使用独立backup系统用户/数据库读取角色，配置TA_BACKUP_DATABASE_URL；不要把这些凭据交给runtime。
`deploy/trading-agent-backup.service/.timer`给出每天UTC02:40的模板，星期日另保留周备份。
由PostgreSQL16的pg_dump生成一致custom archive，SHA256伴随文件，7份日备份/4份周备份。
没有打包角色/表空间；恢复后由管理员重新配置角色及应用授权。

```sh
python -m trading_agent.storage.backups backup --directory /var/backups/trading-agent
python -m trading_agent.storage.backups restore --archive /var/backups/trading-agent/daily-YYYY-MM-DD.dump --expected-database trading_agent_restore_check
```

恢复命令只读取人工环境TA_RESTORE_DATABASE_URL，显式库名必须一致，目标须是新建空库且无runtime持锁。
它不会创建/删除目标库，不用--clean覆盖，不启动runtime。恢复单事务失败回滚；事后再做权限、迁移、
事实/修订链与待验证任务核对。只恢复自己管理的可信备份。
[官方pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)与
[pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)说明对应格式与事务行为。

`deploy/trading-agent-doctor.service/.timer`每5分钟检查，由独立doctor用户持有controller凭据及
自己的通知Profile，状态目录设/var/lib/trading-doctor；不依赖业务runtime存活或数据库。
这些文件是待部署模板，尚未安装、启动或改动任何服务器服务。

## 新增部署核对点

- hermes.python_executable指向同一个独立Hermes环境的Python绝对路径；repo固定提交、源码干净。
  凭据预检查只在该Profile解析凭据，不调用模型、不输出key；独立墙钟预算10秒，
  缓存30秒，成功启动后每20秒后台刷新，readiness不等待冷启动。固定Hermes可用原生安装stamp减少Git版本查询；
  固定提交及源码干净检查仍保留。
  凭据存在不能证明真实模型服务可达，仍须实际前向预测。模型执行失败会使agent健康检查失败。
- 稳定偏好文件实际位置为Profile的memories/USER.md与memories/MEMORY.md，部署锁包含hash。
- controller-settings.yaml只是合并片段；在主控plugins.entries.trading-bridge.settings绑定本runtime的api_url，
  启用插件并向相应平台白名单加入trading_bridge。不要覆盖现有主控配置。
  主控 Profile 继续复用已有 Hermes Gateway 的 TG 消息收发、会话与路由；它与
  hermes.trading_profile 中的预测父/子 Agent 是两个角色，不启动第二个交易 runtime。
  trading_job_create 通过 /v2/jobs 申请当前解释/复盘任务，/v1/jobs 为旧主控兼容路径；
  任务完成通知由已有 outbox 经 HermesSender 调用 controller_profile 发送至 notification.target。
  trading_status、trading_positions 和预测/任务/复盘查询供主控解释事实；不赋予订单或风险修改权限。
  申请/结果闭环与验证边界见[恢复说明](../../docs/controller-hermes-restore-2026-10-22.md)。
- `hermes.trading_telegram_enabled`默认false。需要独立预测Bot时使用与主控不同的Token，
  凭据仅写该预测Profile的.env，两个Bot仍由同一host Gateway托管。
  规范Profile为TG显式配置空工具列表及known_plugin_toolsets，避免Hermes默认加回研究工具；
  CLI研究/Cube工具保持，配置变更后需重建部署锁。当前独立Bot对话不等于已实现只读事实查询、
  自动行情报告或审批回写；正式通知后续仍复用outbox/HermesSender，不把聊天同意当作执行授权。
  本机接入与真实验证见[独立预测TG说明](../../docs/prediction-telegram-2026-10-23.md)。
- Cube SDK0.7数据面只接受127.0.0.1代理，模板的GET详情必须返回cpuCount/memoryMB/diskSizeMB，
  均为正数且不超配置上限；缺字段/超额拒绝。资源容量在模板确定，不假装runtime能动态修改模板。
- 沙箱准备总预算20秒，并受首次模型T+2分钟开始约束；准备耗时也可能使时段跳过，必须在线测量。
- runtime拒绝人工token与迁移/备份/恢复凭据；.env.example是分环境参考列表，不能全部source到runtime。
- `ta status --json`包含24小时运行统计与计量覆盖；parent usage为提交前最后采样。
  原生子结果未返回或进程崩溃时usage可能缺失，不能把样本统计当作完整模型费用账单。
- 三类合约统计的timestamp口径默认未验证。部署时验证区间/瞬时含义后填写market的
  taker_timestamp、oi_history_timestamp、long_short_timestamp；仅code=0不能证明时点正确。
  null状态保留MISSING，不阻止可选特征下的基础预测。所有选择均改变engine hash。
- features.market_basket需明确instId列表，最多50项；成员K线原文与覆盖率保存，空列表不自动补资产。
- runtime.price_refresh_lookback_days默认90、price_refresh_batch_size默认24；每日重查已结算端点。
  这是公共行情读取，使用原预测合约，不访问订单接口。单项最多3次，历史范围和负载需在线测量。
- market.public_ws_url默认`wss://ws.okx.com/ws/v5/public`（443端口）的OKX公开频道，采样无需账户密钥；部署时核对所在地区和代理的实际可达性。
  8443可能先返回64010端口退役通知；采样器在原订阅期限内跳过该通知，仍须收到匹配的订阅确认才记录覆盖。
  清算频道仅为公开采样，20秒文本心跳/5秒确认；断连和重启缺口入库。首次启动前没有可补造的覆盖。
  金额需要合约面值/币种明确，未知规格或查询上限输出MISSING；已采集值仍标PARTIAL_COVERAGE。
  readyz另列liquidation_sampler状态；可选采样断连不阻塞基础预测。连续在线采样和负载还需实测。

预测进程通过本项目的`integrations/hermes/run_hermes.py`进入固定版本Hermes。
该入口仅在预测进程内将插件模块清理改为遍历模块名快照，避免并发导入时遍历活动字典失败；
不修改固定Hermes源码或既有Gateway。入口摘要计入部署锁的`launcher_hash`，打包时与插件一同复制。
升级或回滚时须保持源码、入口和部署锁对应，不可只复制旧锁使校验失配。
