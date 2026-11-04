> 公开脱敏副本：安装路径、内部标识、个人链接及账户/原始业务记录已替换或隐去。历史状态和验证结果仅代表原文记录时点，不构成本仓库当前部署或收益验收。

# Hermes 系统命令入口

日期：2026-10-23（Asia/Shanghai）；仓库 `/srv/open-trade`。

用户在终端执行 `hermes --profile prediction` 提示 `command not found`。
实际 Hermes 已安装于 `.local/private-evidence`，
但系统 PATH 下没有 `hermes` 入口，重新加载 `.bashrc` 无法解决。

本轮新增本机包装器 `/usr/local/bin/hermes`（root 所有，0755）：

```sh
#!/bin/sh
# Local hermes-trade CLI entry; an explicit HERMES_HOME takes precedence.
export HERMES_HOME="${HERMES_HOME:-.local/private-evidence"
exec .local/private-evidence "$@"
```

它复用现有固定 Hermes 安装，原样转发参数，无需激活虚拟环境或修改 `.bashrc`。
必要配套是默认指向项目的 Hermes 根目录，使 `prediction` 能找到此前配置的模型和认证；
如果调用方已显式设置非空 `HERMES_HOME`，仍使用调用方指定的目录。
本机路径只写入本机入口，不向通用 runtime 增加新配置字段。

现在可直接运行：

```sh
hermes --profile prediction
hermes --profile prediction model
hermes --profile prediction config path
```

验证：`sh -n /usr/local/bin/hermes` 通过；全新 Bash 登录 shell 中 `command -v hermes`
返回 `/usr/local/bin/hermes`，`hermes --profile prediction config path` 成功解析到
`.local/private-evidence`；
显式覆盖 `HERMES_HOME` 的独立检查通过，退出码均为 0。

18 份 Profile 配置/认证、OKX/数据库/内部凭据、runtime 草稿及风险执行配置的内容和权限前后不变。
没有调用模型或订单、启动交互会话、迁移 Profile、重启服务或修改其他 session 的代码；
本轮只验证命令入口，没有重跑无关数据库测试，也不把入口修复当作完整模拟上线验收。

证据目录 `.local/private-evidence` 保存入口副本、
保护摘要和验证结果，未复制密钥内容。Feature 继续 Git 忽略，未 commit/push。
回滚仅在确认入口内容仍与本轮副本一致后删除 `/usr/local/bin/hermes`；
保留现有 Hermes 虚拟环境、Profile、认证、服务与其他未提交改动。
