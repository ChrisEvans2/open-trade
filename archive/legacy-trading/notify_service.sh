#!/bin/bash
EVENT=$1
SERVICE_NAME=$2

# Ensure Hermes CLI is available
export PATH=$PATH:/usr/local/bin

if [ "$EVENT" == "start" ]; then
    hermes send --to telegram "🟢 **服务状态更新**\n系统服务 \`$SERVICE_NAME\` 已启动或重启。"
elif [ "$EVENT" == "stop" ]; then
    if [ "$SERVICE_RESULT" == "success" ]; then
        hermes send --to telegram "🔴 **服务状态更新**\n系统服务 \`$SERVICE_NAME\` 已停止 (正常关闭)。"
    else
        hermes send --to telegram "⚠️ **服务告警**\n系统服务 \`$SERVICE_NAME\` 异常退出！\n- 结果: \`$SERVICE_RESULT\`\n- 退出码: \`$EXIT_STATUS\`"
    fi
fi
