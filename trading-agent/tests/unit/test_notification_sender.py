import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from trading_agent.config import AppConfig, HermesConfig, NotificationConfig
from trading_agent.notifications.outbox import HermesSender
from trading_agent.runtime.processes import ProcessResult


def config():
    return AppConfig(
        hermes=HermesConfig(
            executable=Path("/fixture/hermes"),
            controller_profile="controller",
            home=Path("/fixture/home"),
        )
    )


@pytest.mark.parametrize(
    ("payload", "returncode", "truncated", "state", "error"),
    [
        (b'{"success":true,"message_id":123}', 0, False, "SENT", None),
        (b'{"success":true,"skipped":true}', 0, False, "UNKNOWN", "UNRECOGNIZED_SEND_RESPONSE"),
        (b'{"success":true,"partial_success":true}', 0, False, "UNKNOWN", "SEND_UNCONFIRMED"),
        (b'{"error":"Timed out"}', 1, False, "UNKNOWN", "SEND_UNCONFIRMED"),
        (b'{"error":"one chunk failed","message_id":123}', 1, False, "UNKNOWN", "SEND_UNCONFIRMED"),
        (b'{"success":true,"message_id":123}', 1, False, "UNKNOWN", "UNRECOGNIZED_SEND_RESPONSE"),
        (b'{"success":true}', 0, True, "UNKNOWN", "UNRECOGNIZED_SEND_RESPONSE"),
        (b"not-json", 0, False, "UNKNOWN", "UNRECOGNIZED_SEND_RESPONSE"),
        (b"[]", 0, False, "UNKNOWN", "UNRECOGNIZED_SEND_RESPONSE"),
    ],
)
async def test_send_receipt_distinguishes_confirmed_success_from_uncertain_delivery(
    monkeypatch, payload, returncode, truncated, state, error
):
    wait = AsyncMock(return_value=ProcessResult(returncode, payload, b"", truncated))
    start = AsyncMock(return_value=SimpleNamespace(wait=wait))
    monkeypatch.setattr("trading_agent.notifications.outbox.ManagedProcess.start", start)
    monkeypatch.setenv("TA_OPERATOR_TOKEN", "fixture-private")
    result = await HermesSender(config()).send("telegram:fixture", "测试")
    assert (result.state, result.error_code) == (state, error)
    if state == "SENT":
        assert result.message_id == "123"
    argv = start.call_args.args[0]
    assert argv[1:6] == ["-p", "controller", "send", "--to", "telegram:fixture"]
    options = start.call_args.kwargs
    assert options["env"]["HERMES_HOME"] == "/fixture/home"
    assert "TA_OPERATOR_TOKEN" not in options["env"]
    assert options["input_bytes"] == "测试".encode()


async def test_spawn_failure_is_retryable_but_post_spawn_error_is_uncertain(monkeypatch):
    start = AsyncMock(side_effect=FileNotFoundError)
    monkeypatch.setattr("trading_agent.notifications.outbox.ManagedProcess.start", start)
    first = await HermesSender(config()).send("telegram:fixture", "fixture")
    assert (first.state, first.error_code) == ("FAILED", "SEND_PROCESS_START_FAILED")
    start.side_effect = None
    start.return_value = SimpleNamespace(wait=AsyncMock(side_effect=OSError))
    second = await HermesSender(config()).send("telegram:fixture", "fixture")
    assert (second.state, second.error_code) == ("UNKNOWN", "SEND_PROCESS_ERROR")


async def test_startup_cost_is_included_in_send_budget(monkeypatch):
    times = iter((100, 104))
    monkeypatch.setattr("trading_agent.notifications.outbox.monotonic", lambda: next(times))
    wait = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(
        "trading_agent.notifications.outbox.ManagedProcess.start",
        AsyncMock(return_value=SimpleNamespace(wait=wait)),
    )
    result = await HermesSender(config()).send("telegram:fixture", "fixture")
    wait.assert_awaited_once_with(26)
    assert (result.state, result.error_code) == ("UNKNOWN", "SEND_TIMEOUT")


async def test_cancellation_remains_cancellation(monkeypatch):
    monkeypatch.setattr(
        "trading_agent.notifications.outbox.ManagedProcess.start",
        AsyncMock(return_value=SimpleNamespace(wait=AsyncMock(side_effect=asyncio.CancelledError))),
    )
    with pytest.raises(asyncio.CancelledError):
        await HermesSender(config()).send("telegram:fixture", "fixture")


@pytest.mark.parametrize("value", [9, 61])
def test_notification_budget_is_bounded_below_claim_lease(value):
    with pytest.raises(ValidationError):
        NotificationConfig(send_timeout_seconds=value)
    assert NotificationConfig().send_timeout_seconds == 30


async def test_missing_controller_cannot_send(monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr("trading_agent.notifications.outbox.ManagedProcess.start", start)
    result = await HermesSender(AppConfig()).send("telegram:fixture", "fixture")
    assert result.state == "FAILED" and result.error_code == "NOTIFICATION_NOT_CONFIGURED"
    start.assert_not_awaited()
