from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from trading_agent.config import AppConfig, HermesConfig, NotificationConfig
from trading_agent.notifications.predictions import error_message, published


def routed():
    return AppConfig(
        hermes=HermesConfig(
            controller_profile="controller",
            trading_profile="prediction",
            trading_telegram_enabled=True,
        ),
        notification=NotificationConfig(prediction_target="telegram:fixture"),
    )


@pytest.mark.parametrize(
    "action,label",
    [
        ("HOLD", "⏸ 观望"),
        ("OPEN_LONG", "🟢 申请做多"),
        ("OPEN_SHORT", "🔴 申请做空"),
        ("CLOSE", "🟠 申请平仓"),
    ],
)
async def test_parent_summary_leads_with_action_and_keeps_probabilities_and_local_time(
    monkeypatch, action, label
):
    config = routed()
    capture = AsyncMock()
    monkeypatch.setattr("trading_agent.notifications.predictions.enqueue", capture)
    now = datetime(2026, 11, 4, 16, tzinfo=UTC)
    prediction = SimpleNamespace(
        id=uuid4(),
        role="PARENT",
        expires_at=now + timedelta(hours=1),
        report=SimpleNamespace(
            symbol="BTCUSDT",
            structure="BOX",
            p_up=Decimal(".7"),
            p_down=Decimal(".2"),
            p_none=Decimal(".1"),
            reason="第一条依据\n第二条依据",
        ),
    )
    await published(None, config, prediction, SimpleNamespace(action=action), now)
    notice = capture.call_args.kwargs
    body = notice["body"]
    assert body.startswith("📊 BTCUSDT｜父 Agent 预测\n\n结论：" + label)
    assert "首次触发概率\n上涨 70% · 下跌 20% · 均未确认 10%" in body
    assert "11-05 01:00（北京时间）" in body
    assert "第一条依据 第二条依据" in body
    assert "prediction_id=" not in body and "[event:" not in body
    assert notice["include_event_id"] is False


async def test_child_report_never_uses_parent_summary_sender(monkeypatch):
    capture = AsyncMock()
    monkeypatch.setattr("trading_agent.notifications.predictions.enqueue", capture)
    await published(
        None,
        routed(),
        SimpleNamespace(role="TIMEFRAME"),
        None,
        datetime.now(UTC),
    )
    capture.assert_not_awaited()


def test_error_notice_explains_impact_and_actual_retry_time():
    config = AppConfig()
    text = error_message(
        config, "ETHUSDT", "AGENT_TIMEOUT", resume_at=datetime(2026, 11, 4, 16, 1, tzinfo=UTC)
    )
    assert text.startswith("⚠️ ETHUSDT｜预测分析异常\n\n原因：模型分析超时")
    assert "当前父预测暂不可用于开仓" in text
    assert "11-05 00:01（北京时间）自动重试" in text
    assert "错误码：AGENT_TIMEOUT" in text
    assert "job_id=" not in text and "[event:" not in text


def test_final_or_unknown_error_never_promises_another_retry():
    text = error_message(AppConfig(), "BTCUSDT", "FIXTURE_UNKNOWN", affects_parent=False)
    assert "本次任务未完成" in text
    assert "已停止自动重试，需要检查" in text
    assert "FIXTURE_UNKNOWN" in text
