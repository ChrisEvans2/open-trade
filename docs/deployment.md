# 独立部署准备

公共仓库不含运行凭据、安装Profile、隔离报告或业务数据库。示例默认暂停且不完整，不能直接把serve当作安装验收。

1. 在自己的独立目录安装Python3.11、uv和PostgreSQL16；从runtime目录同步锁定依赖。
2. 在仓库外建立配置、受限凭据文件和数据目录。分别保存运行应用、人工迁移、controller/operator及备份身份，不把原始令牌放入模型输入。
3. 明确唯一交易入口，核验备份可恢复，再用人工迁移身份执行`ta --config <配置> migrate`到当前head；运行账号没有迁移权限。用`deploy/provision_permissions.py`配置并验证最小应用权限，保留事实保护。
4. 提供固定Hermes提交`71cb7d60f8469768e597e39d9ddb682a4e20dbfc`和独立的controller/prediction Profile。保持现有Gateway接入分工；配置自己的模型和通知目标。
5. 配置Cube服务/模板、资源预算、网络隔离和显式真实隔离报告。`deploy/prepare_bundle.py`仅生成可审阅包，不自动安装；安装后重新核验Profile、插件、提示词、兼容入口和部署锁。
6. 通过配置、paper-only守卫及部署检查，再按自己的授权过程启动唯一runtime并验收真实行情、模型发布、评分、复盘、通知与资源回收。保留账户风控，不为凑成交清除熔断。

`trading-agent/deploy/*.service`和timer为通用模板，路径、用户及EnvironmentFile均需由部署者配置；本仓库不会自动安装或启动它们。doctor和备份使用独立环境，不混入业务运行凭据。

`config/`仅是新写的暂停模拟示例。共享core不会自动搜索其他安装目录；使用显式参数或TRADING_CONFIG_DIR指定自己的策略目录。当前代码要求0011，事实迁移不做删除历史的downgrade。
