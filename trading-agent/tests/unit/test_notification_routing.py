from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from trading_agent.config import AppConfig, HermesConfig, NotificationConfig
from trading_agent.notifications.outbox import HermesSender
from trading_agent.notifications.routing import destination, prediction_target
from trading_agent.runtime.deployment import expected_lock
from trading_agent.runtime.processes import ProcessResult


def routed():
    return AppConfig(
        hermes=HermesConfig(
            executable=Path("/fixture/hermes"),
            controller_profile="controller",
            trading_profile="prediction",
            trading_telegram_enabled=True,
        ),
        notification=NotificationConfig(
            target="telegram:system", prediction_target="telegram:forecast:7"
        ),
    )


async def test_system_and_prediction_use_different_existing_profiles_with_persisted_route(
    monkeypatch,
):
    config = routed()
    wait = AsyncMock(
        return_value=ProcessResult(0, b'{"success":true,"message_id":123}', b"", False)
    )
    start = AsyncMock(return_value=SimpleNamespace(wait=wait))
    monkeypatch.setattr("trading_agent.notifications.outbox.ManagedProcess.start", start)
    sender = HermesSender(config)
    assert (await sender.send(config.notification.target, "system")).state == "SENT"
    assert (await sender.send(prediction_target(config), "prediction")).state == "SENT"
    first, second = [call.args[0] for call in start.call_args_list]
    assert first[1:6] == ["-p", "controller", "send", "--to", "telegram:system"]
    assert second[1:6] == ["-p", "prediction", "send", "--to", "telegram:forecast:7"]
    # The row retains its exact original destination even if config later changes.
    changed = config.model_copy(
        update={
            "notification": config.notification.model_copy(
                update={"prediction_target": "telegram:new"}
            )
        }
    )
    assert destination(changed, prediction_target(config)) == ("prediction", "telegram:forecast:7")


@pytest.mark.parametrize(
    "value",
    [
        "hermes-profile:other:telegram:fixture",
        "hermes-profile:../prediction:telegram:fixture",
        "hermes-profile:prediction:",
        "hermes-profile:prediction",
    ],
)
async def test_invalid_routes_never_spawn_or_fall_back_to_controller(monkeypatch, value):
    start = AsyncMock()
    monkeypatch.setattr("trading_agent.notifications.outbox.ManagedProcess.start", start)
    result = await HermesSender(routed()).send(value, "fixture")
    assert result.state == "FAILED"
    start.assert_not_awaited()


def test_old_config_and_plain_target_keep_controller_semantics():
    config = AppConfig(
        hermes=HermesConfig(controller_profile="controller"),
        notification=NotificationConfig(target="telegram:old"),
    )
    assert prediction_target(config) == "telegram:old"
    assert destination(config, "telegram:old") == ("controller", "telegram:old")
    assert "prediction_notification_target" not in expected_lock(config)


@pytest.mark.parametrize("enabled,same", [(False, False), (True, True)])
def test_dedicated_prediction_route_requires_its_own_enabled_profile(enabled, same):
    with pytest.raises(ValidationError, match="独立预测Profile"):
        AppConfig(
            hermes=HermesConfig(
                controller_profile="prediction" if same else "controller",
                trading_profile="prediction",
                trading_telegram_enabled=enabled,
            ),
            notification=NotificationConfig(prediction_target="telegram:forecast"),
        )


def test_route_change_is_locked_without_changing_prediction_engine():
    before = routed()
    after = before.model_copy(
        update={
            "notification": before.notification.model_copy(
                update={"prediction_target": "telegram:other"}
            )
        }
    )
    assert expected_lock(before)["prediction_notification_target"] == "telegram:forecast:7"
    assert expected_lock(after)["prediction_notification_target"] == "telegram:other"
    assert expected_lock(before)["engine_hash"] == expected_lock(after)["engine_hash"]
