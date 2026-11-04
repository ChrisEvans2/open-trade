> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Cube 鉴权、真实隔离与预检修复交付

完成日期：2026-10-23（Asia/Shanghai）。工作从北京时间 10 月 22 日开始；本轮本机证据目录按 UTC 日期命名为 `.local/private-evidence`。

仓库为 `/srv/open-trade`。用户授权先处理 Cube 鉴权与隔离，然后修复预检；本轮未启动交易 runtime、Gateway 或真实模型预测，未发送 TG 或调用订单接口。此前未提交的模型配置、代码和两份文档保留；未 commit/push。

## 已完成的 Cube 配置

- 新建独立 Cube 管理 key，以 root:root / 0600 保存在会话 `secrets/cube.env`；同一 key 追加到 `runtime.env`，没有轮换原数据库、controller/operator、模型或 OKX 凭据。CubeAPI 的 `80-hermes-auth.conf` drop-in 只引用凭据文件，不把 key 写入 unit 或命令行。确认当时无活动沙箱后，只重启了 CubeAPI。
- 实测管理接口：无 key=401、错误 key=401、正确 key=200。使用 v0.7.3 自带的 key 鉴权，不新增鉴权网关或回调服务。固定版本实现见 [CubeAPI 鉴权源码](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.3/CubeAPI/src/middleware/auth.rs)。
- 安装 `hermes-cube-boundary.service`，只增加本轮专属 `HERMES-CUBE-IN` / `HERMES-CUBE-FWD` 链。IPv4/IPv6 限制 eth0 上的 Cube 管理/数据端口，转发路径也限制 Docker 发布的 MinIO/WebUI 入口；连接保持本机使用，远程运维通过 SSH 隧道。没有清空、恢复或重写整机防火墙；SSH、其他业务端口、Cube 内部网络及原规则保留。Docker user-chain 另有钩子，避免 Docker 调整 FORWARD 顺序绕过保护。
- CubeAPI 和 CubeProxy 加入边界服务的 Requires/After 依赖，后续启动时边界失败不能继续启动这两个入口。服务定义与依赖已通过 `systemd-analyze verify`；边界脚本连续应用两次验证幂等。CubeAPI 仍绑定原地址，访问限制由防火墙执行，避免破坏已有容器内通信。不能仅凭监听地址或配置字段宣称鉴权生效。
- 当前模拟草稿增加宿主公网 IPv4 /32 的敏感地址拒绝；私网、回环、元数据等原拒绝规则保留。SDK 管理和数据访问继续只走 127.0.0.1；HTTP 数据面不向公网直接开放。处理依据参见 [官方网络加固](https://docs.cubesandbox.com/guide/network-hardening)。

本轮系统文件：

- `/etc/systemd/system/hermes-cube-boundary.service`
- `/usr/local/libexec/hermes-cube-local-boundary`
- `/etc/systemd/system/cube-sandbox-cube-api.service.d/80-hermes-auth.conf`
- CubeAPI / CubeProxy 各自的 `70-hermes-boundary.conf`

非敏感模板保存于仓库 `trading-agent/deploy/cube-local-boundary.sh`、`hermes-cube-boundary.service` 和 `cube-boundary-dependency.conf`，不包含真实 key。

## 真实 guest 隔离修补

旧 `python-basic` 模板有普通用户，但 guest 内未认证的 envd 可以被该用户调用，实测执行 `id -u` 返回 0；原模板还启动 root Jupyter/代码解释器。因此“终端指定普通用户”和“不能 sudo”不足以完成隔离。本轮只在短期验收沙箱验证，没有触及宿主权限或业务事实。

保留旧模板，新增专用模板 **`tpl-d6a492aa8c124d76a9422632`**：

- 使用同一个固定 digest 的公开基础镜像；CPU 1000m、内存 1024 MiB、可写层 1 GiB，未提高现有配置上限。
- 启动脚本为 `trading-agent/deploy/cube-guest-start.py`。只运行 root envd 与降权为 user 的 health-only 服务；不启动 Jupyter、代码解释器或额外模型服务。
- runtime 在上传冻结输入、启动任何模型命令之前，通过 envd `/init` 配置每个沙箱独立生成的临时 token，并验证未认证 RPC 返回 401。token 只存宿主 0600 连接文件，SDK 发送认证头；不写入 guest 环境、任务输入或公开证据。envd 日志收紧为 0600。
- 真实普通用户探测确认：root RPC、token 重置、root 文件接口均拒绝；Jupyter 不监听，代码执行服务不接受执行请求。envd 0.5.11 对 `/init` 已有 token 的保护也在实际沙箱验证，而非仅依据新版本文档推断。
- 新模板无继承 allowOut/L7/宿主卷。第一次专用模板设置了模板 denyOut=0/0，真实研究公网探测失败；由于 Cube 会把模板与任务的 denyOut 追加合并，这一条不能被任务移除。该失败及模板均保留。最终模板以 `allowInternetAccess=false` 为安全默认，不预置 denyOut；runtime 每次创建明确区分正式预测 deny-all 与解释/复盘公网访问。规则合并依据见 [官方网络策略](https://docs.cubesandbox.com/guide/network-policy)。
- 专用模板 DNS 显式使用 1.1.1.1 / 8.8.8.8，避免为私网 DNS 增加 allow 例外。正式预测仍拒绝所有出站；解释/复盘使用公网 DNS/HTTPS，继续拒绝敏感网络。
- `CubeResources.ready()` 同时验证健康、正确 key 可查询、未认证查询被拒绝；不再把公开 `/health` 的 200 当作鉴权正常。

最终模拟草稿仍在 `.local/private-evidence`。只有真实完整检查全部通过后，才更新专用模板、`isolation_verified` 和报告路径；未生成最终部署锁。

## 真实隔离验收与失败保留

`tests/live/test_cube_isolation.py` 为显式 opt-in 验收，使用本机真实 Cube、SDK 0.7.0、固定 Hermes 原生插件，以及 55432 上 pytest 新建的独立临时 PostgreSQL 数据库；不连接 55432 应用库。输入为明确的 acceptance_probe，不发布预测、订单、评分或收益。

检查覆盖 `REQUIRED_ISOLATION_CHECKS` 全部 20 项：普通用户、输入只读、scratch 可写、sudo 拒绝、guest 管理拒绝、无宿主卷、无凭据、文件桥边界、公网研究、敏感地址拒绝、正式出站拒绝、IPv6 禁用、父子共享生命周期、旧 fence 拒绝、归属崩溃恢复、无本机回退、服务端鉴权、受保护传输、无模板继承 allow，以及实际资源限额。

其中，原生 Hermes 验证两个客户端共享同一沙箱、正常子清理不销毁父资源、符号链接不能越过 scratch、缺少 attempt 不回退宿主终端；真实 PG 验证旧 fence 不能续期，新 owner 回收本项目失效资源但保留明确的外来沙箱。最终主动删除的仅是本轮已核对归属的验收沙箱，并确认消失。

中间失败记录原样保存：首轮测试环境用户名不匹配、模板 denyOut 继承导致研究 DNS 失败、正式 deny-all 下系统 DNS 解析超过探针预算，以及补测 DNS 报文的命令编码错误。均在验收代码/配置中修正，没有放宽模型、任务、租约或预测期限，也没有把失败报告改为成功。正式网络探针使用直接 IP 与显式超时的 UDP DNS 报文，避免系统解析器重试掩盖网络策略。

最终通过报告为 `isolation-live-07.json`（20/20、28.69秒），其配置 hash 已与实际草稿复核一致；
原组件同批验收记录在 `pytest-live-05.log`（两个不同用例2 passed、52.17秒）。
另有 `boundary-acceptance.json` 对双栈INPUT/FORWARD、Docker user-chain与实际拒绝规则逐条检查，共15项通过。
没有从另一台互联网主机进行入站连通测试；传输边界以本机内核规则、回环SDK和真实guest探测验证。
各批日志、实际预检和保护文件核对均在上述本机证据目录；报告不是 fixture 改名。部署锁仍须由后续最终 Profile、主控和通知配置共同生成。

## 预检修复

1. 原生认证预检 5–6 秒的主要耗时来自 Hermes 在 import 时查询多次 Git 版本信息。使用固定 Hermes 自带 `scripts/write_install_stamp.py` 生成受支持、被 Git 忽略的安装标识；标识记录真实固定提交、clean 状态和外部维护方式，没有修改 Hermes 源码或认证。独立 Git 提交/干净检查仍保留。
2. 认证子进程使用独立 **10 秒**墙钟预算，只解析配置/认证，不调用模型；失败、无效输出、启动异常和超时都拒绝。该预算不改变模型执行 120 秒、首次预算、预测发布、租约、fence 或沙箱授权。
3. 原有成功/失败缓存上限保持 **30 秒**。成功启动后每 20 秒在后台重新验证，readiness 使用有期限的结果，仍逐次验证部署文件和数据库权限。并发读取不会重复启动探针；过期或失败结果不能保持绿色；停止 runtime 会取消并收束刷新进程。这样低频 doctor 也不会每次都碰到过期缓存，readiness 的外层 2 秒无需放大。
4. 短 Git 进程可能在 psutil 取创建时间前已被回收。现在保留结果和管道输出，缺少创建时间明确记为 None，不编造时间、不向身份不明的 PID/group 发信号。必要的后代回收继续验证 UID、任务 token 摘要和 cwd；未知出生时间时也不能误杀复用 PID。测试强制复现已回收进程，并分别验证已知/未知出生时间的后代回收与无关进程保留。

实际 `prediction` Profile 认证预检成功：首次 **2.447 秒**，缓存读取 **0.000028 秒**；Cube readiness **0.2995 秒**且通过。真实结构预测仍未执行。整体部署文件检查继续返回 `DEPLOYMENT_CONFIGURATION_INCOMPLETE`，原因是主控/消息目标/最终锁尚未配置，未绕过该门禁。

## 验证结果

- 全部当前单元、固定 Hermes 合约，以及真实 PG 的部署身份、Cube 生命周期、预测 runner、结构 runtime：**169 passed，179.05 秒**。模型/行情在这些程序回归中使用明确替身，PostgreSQL 为真实独立实例，不等同于真实模型验收。
- 此前两个原生认证用例的 5 秒上限保持原样，现均通过；未改测试等待时间掩盖性能问题。
- 真实 Cube 全隔离和原组件续期/原生终端：两个不同用例均通过；全隔离检查另补测正式 UDP DNS 禁止出站。
- 认证缓存后台刷新补充独立用例后，所在文件 **6 passed，0.54秒**，其中5项已包含在上述169项；最终共 **170个不同用例** 已通过：149单元、5 Hermes、16真实PG。没有重复叠加此前39项定向回归。
- Ruff、格式、`git diff --check`、shell 语法与 systemd 定义检查通过。最终只读核对应用库仍为50表、仅runtime_control初始化1行；原保护文件无变化，runtime.env仅增加Cube key，未含凭据的文档/差异检查通过。Hermes源码仍clean，验收沙箱为0；本轮启动的58917测试PG已正常停止，行情、58918应用库、备份timer与Cube服务保持active。完整命令/日志与最终安全复核保存在证据目录。

回归命令（`/srv/open-trade/trading-agent`）：

```sh
TA_TEST_DATABASE_URL=<仅独立测试库管理连接> \
TA_HERMES_REFERENCE=.local/private-evidence \
.venv/bin/pytest -q tests/unit tests/hermes \
  tests/integration/test_deployment_identity.py \
  tests/integration/test_cube_lifecycle.py \
  tests/integration/test_prediction_runner.py \
  tests/integration/test_structural_runtime.py
```

真实外部验收需显式提供 `CUBE_API_KEY`（从受限文件在进程内加载，不打印）、`TA_CUBE_LIVE_CONFIG`、**新的** `TA_CUBE_LIVE_REPORT` 和独立 `TA_TEST_DATABASE_URL`，执行 `tests/live/test_cube_isolation.py`；报告路径存在即拒绝覆盖。原组件用例另需 `TA_CUBE_LIVE_TEMPLATE` / `TA_HERMES_REFERENCE`。不要把整份运行/迁移/备份凭据合并 source 到模型环境。

## 本轮自主选择、限制与回滚

必要配套选择为：独立 Cube key、服务 drop-in、仅本机的端口边界、专用安全模板、公网 DNS、每个 guest 的临时 envd 认证、真实外部验收脚本、认证后台刷新，以及缺失进程身份的保守处理。全部用于完成本次鉴权/隔离与预检，没有新增业务 Agent、TG 网关、交易入口或收益模型。

仍未完成：controller Profile / 旧 TG 资料迁移、通知目标、最终部署锁、唯一交易 runtime 启动和真实模型预测→风控→模拟成交→复盘。PaperTransport、原风险限制、模型选择与既有备份继续沿用。本轮没有预测准确度、收益或线上交易结论。

回滚必须逐项对照证据目录 `before/`，保留后来变更：

- 代码只撤回本轮差异，保留先前模型配置；runtime 草稿只恢复本轮 sandbox 模板/拒绝地址/隔离字段。恢复旧模板时必须同时回到 `isolation_verified=false`，不能沿用新模板的验收。
- 关闭任何未来使用本 key 的 runtime 后，才撤销本轮 CubeAPI auth drop-in / runtime 新增变量，并只重启 CubeAPI。不要覆盖整个 runtime.env 或轮换已有身份。新 key、历史证据和模板保留为受限本地资料；不要通过清库回滚。
- 停止/禁用边界 unit 本身保留现有防护。只有明确需要撤销本轮规则时执行 `/usr/local/libexec/hermes-cube-local-boundary remove eth0`，只移除本轮两条链及对应钩子；不恢复整份旧 iptables 快照。移除 CubeAPI/Proxy 的新增依赖前应先确认其他会话未复用此边界。
- 原生 install-stamp 是本轮新增的本地安装标识，可对照固定提交移除本轮文件；保留 Hermes 源码和认证。随后冷启动会重新承担 Git 查询耗时，但认证预算和失败保护仍有效。
- 不删除已发布事实、历史评分、行情、备份、原模板或原失败证据；Feature 继续忽略，changelog 未提前归档。


## 后续模拟部署验收

本报告之后已生成最终锁、部署唯一paper runtime，并完成真实模型预测、既有风控、模拟开平仓和模型复盘。实际事实、修复、测试和限制见[模拟 runtime 部署与闭环验收](paper-runtime-deployment-2026-10-23.md)；本报告原有未验收项保留为当时记录。
