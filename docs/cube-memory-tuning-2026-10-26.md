> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Cube 常驻内存优化与双沙箱容量验收

日期：2026-10-26。仓库 `/srv/open-trade`，Cube v0.7.3；仅模拟环境。

用户授权在保留隔离和现有功能前提下优化 Cube，并验收两个独立沙箱并行。维护前唯一交易 runtime 已为 inactive，Cube 活动沙箱为 0；本轮不启动业务 runtime、不改交易并发、不调用模型/订单、不发送 TG。既有开发会话、其他项目、行情、应用 PostgreSQL、备份和双 Profile Gateway 保留。

本机受限证据目录：`.local/private-evidence`。包含改前配置、Cube MySQL 完整逻辑备份、保护文件摘要、逐步内存快照与原始测试记录；不提交密钥、SQL、运行数据或原始 API 响应。

## 实际变更

| 部位 | 原值 | 新值及边界 |
| --- | --- | --- |
| Cubelet TAP 预建池 | 500 | 16；保留原有按需创建、回收、默认拒绝和 BPF 策略路径 |
| Cubelet cgroup 预建池 | 3000 | 32；保留按需分配、CPU/内存限制和所有 VM overhead 参数 |
| CubeProxy | 4 worker、每 worker 100000 连接 | 2 worker、每 worker 4096 连接；监听、HTTP/gRPC、流式代理、鉴权、路由、缓存区和防火墙不变 |
| Cube MySQL | 默认摘要/长历史各 10000、表缓存 4000/2000 | 摘要/长历史各 1024、表缓存各 512；Performance Schema 保持 ON，保留所有 instrument 类别和短历史 |
| Cubelet Go 堆 | 默认无软目标 | `GOMEMLIMIT=160MiB`，只调整 Go GC；不设置进程硬杀限制、不改变 guest 内存 |

MySQL 的 128 MiB InnoDB buffer pool、最大连接 151、`innodb_flush_log_at_trx_commit=1`、`sync_binlog=1`、身份、数据卷和所有业务表均保持原配置。缩短的是易失性诊断历史的保留条数，诊断查询仍可用；不是清理数据库事实。

宿主机实际文件：

- `/usr/local/services/cubetoolbox/Cubelet/config/config.toml`：仅两个预建数量。
- `/etc/systemd/system/cube-sandbox-cubelet.service.d/50-hermes-memory.conf`：Go 软目标。
- `/usr/local/services/cubetoolbox/cubeproxy/nginx.conf` 及 `.template`：仅 worker/连接数；同步模板避免下次服务启动覆盖。
- `/usr/local/services/cubetoolbox/support/hermes-memory.cnf`：只读挂载至 MySQL 的 `/etc/mysql/conf.d/hermes-memory.cnf`。
- `support/docker-compose.yaml` 及 `.template`：仅新增上述非敏感 cnf 挂载，沿用原有数据卷和环境配置。

仓库可复用参数为 [cube-memory-profile.json](../trading-agent/deploy/cube-memory-profile.json)、[MySQL 选项](../trading-agent/deploy/cube-mysql-memory.cnf)、[Cubelet GC drop-in](../trading-agent/deploy/cubelet-memory.conf)。这些文件不是自动安装器；升级 Cube 后须核对渲染模板是否仍保留修改。

## 空闲资源回收和安全边界

修改前反复核实：交易 runtime inactive、管理 API 无活动沙箱、两个网络 owner 状态目录为空。先停止 Cubelet，再只回收本节点 Cube CIDR 内 484 个多余闲置 TAP；保留 16 个。网络地址与原内核策略不改，不对整机网络做清空或重建。

第二次空闲停止时，只对 `/sys/fs/cgroup/cube_sandbox/sandbox/` 下数字编号 32～2999、无子目录、`cgroup.procs` 为空、`cgroup.events populated=0` 的 2968 个旧池目录执行 rmdir。没有删除模板、快照、磁盘镜像、任务事实或其他项目资源。后续原生测试按需增加少量 TAP/cgroup 属正常行为，预建目标不是强制删除活跃资源的上限。

Go 堆探测发现约 103 MB 活跃分配、约 132 MB 空闲堆但仅约 2.5 MB 已交还系统，因而追加软目标以改善回收。Go 目标是软约束，会增加部分 GC 工作；不能把它解释为进程总内存硬上限，也不覆盖 BPF、mmap 或 guest 内存。原有约 280 MiB 的 BPF 哈希分配仍保留，未通过缩减/绕过网络隔离表追求数字下降。

维护收尾发现原生 `cube-sandbox-cube-egress.service` 对 Cubelet 有 Requires 依赖：显式停止 Cubelet 会联动停止 Egress，随后仅启动 Cubelet 不会自动恢复该依赖方。本轮已显式启动 Egress，保留原生服务关系，并逐项核对 13 个 Cube 服务的 ActiveState。之前 Egress 暂停阶段的通过记录仅作为阶段证据，最终必须在全部组件 active 后重跑。容量测试现额外断言 API/Cubelet/Proxy/Egress 在验收前后均 active；运维时不要只凭批量 `is-active` 的总返回码判断所有服务正常。

未改变沙箱模板、1 vCPU/1024 MiB/1 GiB 可写层规格、envd 临时认证、普通用户、冻结输入只读、scratch 权限、禁止宿主挂载、IPv6 禁用、正式任务拒绝出站、研究敏感地址拒绝、lease/fence/任务期限或既有风控。

## 测量口径

进程采用 `/proc/*/smaps_rollup` 的 PSS，避免 PostgreSQL 等共享页重复相加。服务 cgroup 采用互不重叠的根目录，另外包含缓存和内核记账。服务重启会迁移部分内存记账，故不能仅靠 cgroup 数字变化断言物理内存释放量；文件缓存也不应当全部算作不可回收工作集。

改前基线 `baseline.json`：Cube 进程 PSS 1361.22 MiB，整机 MemAvailable 2162.47 MiB，Cube cgroup 1925.88 MiB。该初版快照的 TAP 计数器使用旧命名规则，0 是计数器缺陷；随后 `ip -j -d link` 确认实际为 500，原快照保留。

第一阶段约 MySQL 500→310～330 MiB、代理 180→17 MiB。追加 Go 软目标后 `after-gc-tuning.json`：Cube 进程 PSS 909.01 MiB，MemAvailable 2996.30 MiB。整机差额也受其他持续工作的 Codex/VS Code 会话影响，不全部记为 Cube 的优化收益。

## 验收、失败记录与限制

第一阶段单元、真实 PostgreSQL、固定原生 Hermes 和真实 Cube 隔离/续期合计 **31 passed / 85.84 秒**。全隔离报告覆盖 20 项检查。临时 PostgreSQL 55432 与正式应用库 55432 分离。

新增 [test_cube_capacity.py](../trading-agent/tests/live/test_cube_capacity.py) 为显式 opt-in 真沙箱容量测试：

- 连续 3 轮，每轮两个不同 ID 的原模板沙箱，各写入并持续保有 512 MiB 内存 45 秒；共同运行时验证相同路径的 owner 标记互不覆盖。
- 宿主另开两个各持有 256 MiB 的合成进程，模拟 Agent 开销；没有调用真实模型，也不生成预测、订单或收益。
- 每秒采集 MemAvailable、全机 OOM 计数和 memory PSI；要求最少保留 768 MiB、OOM 无增量、full avg10 < 1%。期间核对归属和续期，结束后仅销毁本测试拥有的沙箱。
- 测试报告含成功和失败轮次，不覆盖历史报告。保护失败时立即回收本轮资源；不靠增加模型预算或关闭隔离通过测试。

第一阶段容量测试 `capacity-live.json` **未通过**：两条 guest 命令完成，无 OOM，但 full PSI avg10 最高达到 4.34%，超出预设 1% 门槛。原日志和失败报告保留；没有降低门槛。随后才追加上述 Go 软目标，并补充容量报告对失败轮次的采样保留。

在全部 13 个 Cube 服务 active 的最终状态下，原 31 项回归再次 **31 passed / 97.61 秒**；容量测试 **1 passed / 186.29 秒**。最终共 **32 个不同用例通过**，不重复累计之前的阶段回归。

| 双沙箱轮次 | 每个 guest / 每个宿主模拟进程 | 最低 MemAvailable | 最高 full PSI avg10 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 512 MiB / 256 MiB | 1348.3 MiB | 0.32% | 两条命令成功，owner 标记互不覆盖 |
| 2 | 512 MiB / 256 MiB | 1392.7 MiB | 0.38% | 同上 |
| 3 | 512 MiB / 256 MiB | 1439.2 MiB | 0.00% | 同上 |

三轮均无 OOM 增量、无沙箱丢失，续期成功，资源归属核验和销毁通过。`capacity-all-services.json` 保存每秒样本及关键组件前后状态；`isolation-all-services.json` 为最终真实隔离 20/20，旧失败报告未覆盖。55432 临时 PostgreSQL 已停止。

最终空载快照 `final-idle-all-services.json`（04:28:22 UTC，所有 13 个组件 active）：

| 指标 | 改前 | 最终 |
| --- | --- | --- |
| Cube 进程 PSS | 1361.22 MiB | 956.99 MiB（约 0.93 GiB，减少 404.23 MiB / 29.7%） |
| 整机 MemAvailable | 2162.47 MiB | 3164.50 MiB |
| 宿主不可回收 slab | 385.38 MiB | 202.40 MiB |
| 活动沙箱 | 0 | 0，所有验收资源已回收 |

宿主指标受其他持续开发进程影响，不将全部增加的可用内存或 slab 差额当作 Cube 单项收益。最终 cgroup 内存 1809.77 MiB 含约 920.09 MiB 文件缓存；不能与 PSS 相加，也不能据此宣称占用已经全部下降到 0.93 GiB。网络 BPF 等内核开销仍存在。

15 个凭据/Profile/运行配置文件摘要与权限全部不变；原 Cube 安装环境不变；最新 `protection-all-services.json` 另逐项确认全部 13 个服务 active。结构化差异核对确认：Cubelet TOML 只改两个预建数量，代理只改两个参数，Compose 仅增加 MySQL cnf 挂载。沙箱模板 createRequest 在剔除每次查询动态生成的 requestID 后完全一致。管理 API 无 key/错误 key/正确 key 为 401/401/200；Go 软目标在实际进程环境生效。Gateway PID（私有进程标识已隐去） 未变，主控与预测通道未重启，行情、正式 PostgreSQL、Cube 组件均保持 active。Ruff、格式、JSON 解析、systemd 定义、`git diff --check` 通过。

**验收结论：在上述负载和当前其他服务保持运行的条件下，Cube 双沙箱并行通过三轮测试，最低仍有约 1.32 GiB 可用内存。** 短时合成负载结果不能承诺所有未来模型/数据规模、满 1 GiB guest 工作集或长期运行都能稳定。业务 runtime 仍保持原模型任务并发 1，调整交易任务调度与部署锁不属于本次 Cube 服务优化。本轮未增加 Swap，也未手动清空系统缓存来改变测试结果。

复验在 `/srv/open-trade/trading-agent` 执行；key 仅由受限加载器写入当前进程环境，不能复制到命令行或报告中：

```sh
# 此处环境须预先安全加载 CUBE_API_KEY；两类验收报告必须使用新路径。
TA_TEST_DATABASE_URL=<独立临时PostgreSQL管理连接> \
TA_CUBE_LIVE_CONFIG=<当前模拟config.yaml绝对路径> \
TA_CUBE_LIVE_REPORT=<新的真实隔离报告绝对路径> \
TA_CUBE_LIVE_TEMPLATE=tpl-d6a492aa8c124d76a9422632 \
TA_CUBE_LIVE_USER=user \
TA_HERMES_REFERENCE=.local/private-evidence \
.venv/bin/pytest -q tests/unit/test_sandboxes.py \
  tests/integration/test_cube_lifecycle.py tests/hermes \
  tests/live/test_cube_isolation.py tests/live/test_cube_connection.py

# 先停止临时测试库；确认唯一业务runtime停止且Cube无其他活动沙箱。
TA_CUBE_CAPACITY_CONFIG=<当前模拟config.yaml绝对路径> \
TA_CUBE_CAPACITY_REPORT=<新的容量报告绝对路径> \
.venv/bin/pytest -q tests/live/test_cube_capacity.py
```

## 回滚

在停止使用这些资源的唯一业务 runtime、确认无活动沙箱后，逐项恢复本轮参数；保留其他会话后续改动。

1. Cubelet 将 TAP/cgroup 预建值恢复为 500/3000；移除本轮 `50-hermes-memory.conf`，daemon-reload 后重启 Cubelet，并显式 `systemctl start cube-sandbox-cube-egress.service`，逐项核对相关服务 active。原生后台预建会恢复池，不需要恢复已回收的空内核对象。
2. Proxy 的实际文件和渲染模板恢复 auto/100000，`nginx -t` 后 reload。
3. MySQL 的模板和产物仅移除本轮 cnf 挂载，重启原 unit；保留原数据卷和凭据。不要把 SQL 备份自动回灌，不回滚或覆盖已有事实。
4. 仓库只撤回本轮新增参数/测试/说明，保留此前其他未提交改动。测试报告和失败证据保留。

## 参数依据

- [固定 v0.7.3 官方性能说明](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.3/docs/blog/posts/2026-06-01-cubesandbox-perf-benchmark.md)：TAP 预建参数与负载相关，官方大机测试不能替代本机容量验收。
- 固定版本源码 [网络运行时](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.3/Cubelet/network/runtime/controller.go) 与 [cgroup 池](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.3/Cubelet/plugins/cube/internals/cgroup/pool.go)：复用、按需创建、归属恢复及清理路径。
- [NGINX 连接数](https://nginx.org/en/docs/ngx_core_module.html#worker_connections) 与 [重载行为](https://nginx.org/en/docs/control.html)：连接数按 worker 计，重载先验证并等待旧 worker 结束。
- [MySQL 8 Performance Schema 启动参数](https://docs.oracle.com/cd/E17952_01/mysql-8.0-en/performance-schema-startup-configuration.html)：诊断容量与内存相关；保持仪表功能开启。
- [Go GC 软内存目标](https://go.dev/doc/gc-guide#Memory_limit)：目标覆盖 Go 管理内存，可用 GC 时间换取较低堆占用。
