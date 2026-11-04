> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# 本机 Cube 自行部署交接

2026-10-17（Asia/Shanghai）。用户决定自行尝试部署；开发 Agent 已停止后续部署，不执行 Cube 安装脚本、创建模板或接入模型。

## 当前实际运行情况

仅 `open-trade-paper-market.service` 继续运行：每小时读取 BTC、ETH 的 1h/4h/6h/12h 已收盘 K 线，计算指标、结构候选，保存公共行情证据。

预测 Agent、预测复盘、正式风控执行链和模拟成交没有启动。没有真实订单，没有准确率或收益结论。数据目录为 `.local/private-evidence`。

## 已准备的内容

- 官方 Cube v0.7.3 amd64 安装包：`.local/private-evidence`。
- 已解压目录：`.local/private-evidence`。
- 安装包 SHA256 已与 GitHub 发布 API 的 digest 一致核对：`<private-artifact-hash>`。
- Docker Engine/CLI 26.1.5、Compose 2.26.1、XFS 工具已安装。没有创建容器；安装工具引入的 Docker、socket、containerd 及 XFS scrub timer 已停止并取消开机启用，不影响已有项目服务。
- 已创建独立 32 GiB **稀疏文件镜像** `.local/private-evidence`，格式化对象仅为这个新文件。其容量不等于立即实际占用 32 GiB。原 ext4 分区没有格式化。
- 挂载单元 `/etc/systemd/system/data-cubelet.mount` 已准备，曾实测挂载成功；现在已卸载，未设开机自启，`/data/cubelet` 目录为空。
- 已读取安装包脚本并保存路由、DNS、iptables、服务基线和工具安装日志，均在 `.local/private-evidence`。`inspect/` 只是安装包内脚本/配置的检查副本，不是已部署服务。
- `install.sh` **没有执行**。不存在正在运行的 Cube API、CubeMaster、Cubelet、模板或沙箱。没有修改 DNS 路由或安装 Cube 防火墙规则；Docker 软件包启动阶段曾建立其默认网络规则，因此保存了安装前基线。没有刷新或恢复整机 iptables，避免影响已有业务。

## 自行操作时的起点

先手动启动已经准备好的依赖与挂载：

```bash
sudo systemctl start containerd.service docker.service data-cubelet.mount
findmnt /data/cubelet
docker compose version
cd .local/private-evidence
```

然后按包内 `README.md`、`env.example` 与官方部署文档配置 `.env`，再运行其安装脚本。样例数据库/Redis 密码需要替换，新建 Cube 管理 key；不要把迁移目录的大份 `.env` 整份复制进来。原认证文件和旧交易配置继续保持不变。

本机配置应明确管理接口仅监听回环地址：`CUBEMASTER_HTTP_BIND=127.0.0.1`、`CUBE_API_BIND=127.0.0.1:3000`、`CUBE_OPS_BIND=127.0.0.1:3010`，并设置 `CUBE_API_KEY`；使用回环 CubeOps 时先设 `WEB_UI_ENABLE=0`，避免包内 WebUI 容器依赖公网绑定。代理数据面也需检查实际监听和访问限制，安装后核对 `ss -lntup`，不要直接照搬默认公开监听配置。

本机已有 systemd-resolved，当前 `127.0.0.54:53` 是其监听地址；安装包支持 resolved 的独立路由域配置。操作时检查实际 DNS 绑定，保留现有业务域名解析，不直接覆盖全局 `/etc/resolv.conf`。Cube 安装器会引入更多系统服务及网络配置，上面的准备命令不等于完整部署。

官方参考：[部署说明](https://docs.cubesandbox.com/guide/bare-metal-deploy)、[网络限制](https://docs.cubesandbox.com/guide/network-hardening)、[固定版本发布](https://github.com/TencentCloud/CubeSandbox/releases/tag/v0.7.3)。

## 安装完成后仍需接通的内容

交易程序固定使用 Cube Python SDK 0.7.0，需要实际验证与本次 Cube 服务的接口兼容性、模板资源字段、管理端认证和回环数据面。模型所用模板还须符合普通用户、只读冻结输入、无宿主挂载、无凭证、网络限制和任务恢复等要求，并保存真实隔离验收证据；不能用测试 fixture 代替。

随后才能完成独立 Hermes Profile、模型认证范围、正式 PostgreSQL 16 应用库与权限、部署锁、仅本地保存的运行通知/导出策略及 PaperTransport 执行。旧交易 CLI 不参与这次模拟。原结构 runtime 的 15 秒 fixture 超时仍需按实际环境核对；延长测试等待的诊断结果不算标准用例通过。

已有固定版本 Hermes 代码及核心依赖位于 `.local/private-evidence`，交易程序依赖位于 `/srv/open-trade/trading-agent/.venv`；本次没有把迁移认证复制或接入进去。

## 停止与保留

只停止当前行情采集可执行：

```bash
sudo systemctl stop open-trade-paper-market.service
```

独立镜像、安装包、挂载单元和证据均保留供用户使用。现阶段不需要清空磁盘、恢复整机防火墙或删除数据。未 commit/push。
