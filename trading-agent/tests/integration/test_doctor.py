import httpx

from trading_agent.config import AppConfig, NotificationConfig
from trading_agent.notifications.outbox import Delivery
from trading_agent.runtime.doctor import doctor


class Sender:
    def __init__(self):
        self.messages = []

    async def send(self, target, body):
        self.messages.append((target, body))
        return Delivery("SENT")


async def test_external_doctor_incident_is_durable_quiet_and_recovers_once(monkeypatch, tmp_path):
    state, sender = {"ready": False}, Sender()

    async def fixture_health(config, token):
        assert token == "fixture-controller"
        return {
            "ready": state["ready"],
            "code": "READY" if state["ready"] else "RUNTIME_UNREACHABLE",
        }

    monkeypatch.setattr("trading_agent.runtime.doctor.check_runtime", fixture_health)
    config = AppConfig(notification=NotificationConfig(target="fixture-target"))
    options = {"notify": True, "sender": sender, "state_path": tmp_path / "doctor.json"}
    assert (await doctor(config, "fixture-controller", **options))["notification"] == "SENT"
    assert (await doctor(config, "fixture-controller", **options))["notification"] == "QUIET"
    state["ready"] = True
    assert (await doctor(config, "fixture-controller", **options))["notification"] == "SENT"
    assert (await doctor(config, "fixture-controller", **options))["notification"] == "QUIET"
    assert len(sender.messages) == 2
    assert (tmp_path / "doctor.json").stat().st_mode & 0o077 == 0
    assert "fixture-controller" not in (tmp_path / "doctor.json").read_text()


async def test_doctor_wrong_identity_is_failure(monkeypatch):
    from trading_agent.runtime.doctor import check_runtime

    native = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: native(
            **kwargs,
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {}})),
        ),
    )
    result = await check_runtime(AppConfig(), "fixture-invalid-token")
    assert result == {"ready": False, "code": "DOCTOR_CREDENTIAL_REJECTED"}
