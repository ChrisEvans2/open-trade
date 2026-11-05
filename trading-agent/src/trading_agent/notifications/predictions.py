"""Parent prediction summaries; status transitions stay in the prediction records."""

from zoneinfo import ZoneInfo

from trading_agent.notifications.outbox import enqueue
from trading_agent.notifications.routing import prediction_target


async def published(connection, config, prediction, decision, now):
    if config.notification.prediction_target is None or prediction.role != "PARENT":
        return  # opt in to result notices with the dedicated destination
    report = prediction.report
    action = {
        "HOLD": "⏸ 观望",
        "OPEN_LONG": "🟢 申请做多",
        "OPEN_SHORT": "🔴 申请做空",
        "CLOSE": "🟠 申请平仓",
    }[decision.action]
    structure = {
        "UNKNOWN": "未确认结构",
        "BOX": "箱体",
        "W_BOTTOM": "W底",
        "ASCENDING_TRIANGLE": "上升三角",
        "CONVERGING_TRIANGLE": "收敛三角",
    }[report.structure]
    expiry = prediction.expires_at.astimezone(ZoneInfo(config.notification.display_timezone))
    reason = " ".join(report.reason.split())
    if len(reason) > 140:
        reason = reason[:140] + "…"
    await enqueue(
        connection,
        key=f"prediction-published:{prediction.id}",
        target=prediction_target(config),
        body=f"📊 {report.symbol}｜父 Agent 预测\n\n"
        f"结论：{action}\n结构：{structure}\n\n"
        f"首次触发概率\n上涨 {report.p_up:.0%} · 下跌 {report.p_down:.0%} · "
        f"均未确认 {report.p_none:.0%}\n\n"
        f"🕒 有效至 {expiry:%m-%d %H:%M}（北京时间）\n\n"
        f"依据：{reason}\n\n交易须经风控确认。",
        now=now,
        include_event_id=False,
    )


def error_message(config, symbol, code, *, resume_at=None, affects_parent=True):
    reason = {
        "AGENT_TIMEOUT": "模型分析超时",
        "ANALYSIS_DEADLINE": "本轮分析超过总时限",
        "DEPENDENCY_UNAVAILABLE": "运行依赖暂时不可用",
        "CUBE_UNAVAILABLE": "分析沙箱暂时不可用",
        "BOUNDARY_REFERENCE": "分析引用的价格边界未通过校验",
        "CHILD_REPORT_INVALID": "子周期分析未通过校验",
    }.get(code, "分析未能完成")
    impact = "当前父预测暂不可用于开仓" if affects_parent else "本次任务未完成"
    if resume_at is None:
        following = "已停止自动重试，需要检查"
    else:
        when = resume_at.astimezone(ZoneInfo(config.notification.display_timezone))
        following = f"将在 {when:%m-%d %H:%M}（北京时间）自动重试"
    return (
        f"⚠️ {symbol}｜预测分析异常\n\n"
        f"原因：{reason}\n影响：{impact}\n\n"
        f"后续：{following}\n错误码：{code}"
    )
