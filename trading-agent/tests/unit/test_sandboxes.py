import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from trading_agent.config import AppConfig, SandboxConfig
from trading_agent.domain.errors import DomainError
from trading_agent.runtime.sandboxes import CubeResources, input_files, sandbox_policy


def test_formal_network_has_no_allow_holes_and_research_blocks_explicit_public_service():
    config = SandboxConfig(denied_networks=("203.1.2.3",))
    formal = sandbox_policy(config, "FORECAST")
    research = sandbox_policy(config, "REVIEW_DAILY")
    assert formal["allow_internet_access"] is False
    assert "0.0.0.0/0" in formal["network"]["denyOut"]
    assert formal["network"]["allowOut"] == [] and formal["network"]["rules"] == []
    assert research["allow_internet_access"] is True
    assert "203.1.2.3/32" in research["network"]["denyOut"]
    assert "169.254.0.0/16" in research["network"]["denyOut"]
    assert research["network"]["allowPublicTraffic"] is False


def test_input_bridge_rejects_symlink_and_unknown_host_file(tmp_path):
    folder = tmp_path / "input"
    folder.mkdir()
    secret = tmp_path / "credential"
    secret.write_text("fixture-secret")
    (folder / "context.json").symlink_to(secret)
    with pytest.raises(OSError):
        input_files(tmp_path)
    (folder / "context.json").unlink()
    (folder / "credential").write_text("fixture-secret")
    with pytest.raises(DomainError) as exc:
        input_files(tmp_path)
    assert exc.value.code == "SANDBOX_INPUT_INVALID"


def test_policy_refuses_ipv6_cidr_in_ipv4_sdk_field():
    with pytest.raises(DomainError) as exc:
        sandbox_policy(SandboxConfig(denied_networks=("fd00::/8",)), "EXPLAIN")
    assert exc.value.code == "SANDBOX_NETWORK_INVALID"


def test_renewal_config_requires_room_for_control_calls_and_cleanup():
    with pytest.raises(ValidationError, match="沙箱超时必须覆盖"):
        SandboxConfig(
            idle_timeout_seconds=120,
            renew_interval_seconds=60,
            management_timeout_seconds=15,
            timeout_grace_seconds=30,
        )


@pytest.mark.parametrize("capacity", ["8192", "0", "unknown"])
def test_guest_disk_measurement_rejects_unbounded_or_unknown_storage(
    tmp_path, monkeypatch, capacity
):
    class Sandbox:
        def __init__(self, *args):
            self.commands = self

        def run(self, command, **kwargs):
            return SimpleNamespace(exit_code=0, stdout=capacity)

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules, "cubesandbox", SimpleNamespace(Config=lambda **kwargs: kwargs, Sandbox=Sandbox)
    )
    resources = SimpleNamespace(config=AppConfig(), sdk_settings={})
    monkeypatch.setattr(CubeResources, "_secure_guest", lambda self, raw: None)
    with pytest.raises(DomainError) as caught:
        CubeResources._populate(resources, {}, tmp_path)
    assert caught.value.code == "CUBE_RESOURCE_LIMITS"


@pytest.mark.parametrize("init_status,probe_status", [(500, 401), (204, 200), (204, 401)])
def test_guest_authentication_is_required_before_exposing_task_data(
    monkeypatch, init_status, probe_status
):
    class Sandbox:
        def __init__(self, *args):
            pass

        def get_host(self, port):
            return "fixture-sandbox-" + str(port) + ".cube.app"

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules, "cubesandbox", SimpleNamespace(Config=lambda **kwargs: kwargs, Sandbox=Sandbox)
    )
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, path, **kwargs):
            calls.append((path, kwargs))
            return SimpleNamespace(status_code=init_status if path == "/init" else probe_status)

    monkeypatch.setattr(httpx, "Client", Client)
    raw = {"sandboxID": "fixture", "trafficAccessToken": "fixture-traffic"}
    resources = SimpleNamespace(config=AppConfig(), sdk_settings={"api_key": "fixture-host-key"})
    if init_status == 204 and probe_status == 401:
        CubeResources._secure_guest(resources, raw)
        assert raw["envdAccessToken"] == calls[0][1]["json"]["accessToken"]
        assert len(raw["envdAccessToken"]) >= 48
        assert "X-Access-Token" not in calls[1][1]["headers"]
    else:
        with pytest.raises(DomainError) as caught:
            CubeResources._secure_guest(resources, raw)
        assert caught.value.code == "CUBE_GUEST_AUTH_FAILED"
        assert "envdAccessToken" not in raw


async def test_management_request_has_total_wallclock_timeout():
    pytest.importorskip("cubesandbox")

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    config = AppConfig(
        sandbox=SandboxConfig(
            base_url="http://127.0.0.1:3000",
            template="fixture",
            sdk_version="0.7.0",
            proxy_node_ip="127.0.0.1",
            management_timeout_seconds=0.02,
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow), base_url=config.sandbox.base_url
    )
    resources = CubeResources(config, "fixture-key", client=client)
    try:
        with pytest.raises(DomainError) as caught:
            await resources.request("POST", "/sandboxes/fixture/timeout", {"timeout": 30})
        assert caught.value.code == "CUBE_UNAVAILABLE"
    finally:
        await resources.close()


@pytest.mark.parametrize("enforced,valid_key", [(True, True), (False, True), (True, False)])
async def test_readiness_requires_working_key_and_server_side_enforcement(enforced, valid_key):
    def server(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        accepted = valid_key and request.headers.get("X-API-Key") == "fixture-key"
        return httpx.Response(200, json=[]) if accepted or not enforced else httpx.Response(401)

    config = AppConfig(
        sandbox=SandboxConfig(
            base_url="http://127.0.0.1:3000",
            template="fixture",
            sdk_version="0.7.0",
            proxy_node_ip="127.0.0.1",
        )
    )
    client = httpx.AsyncClient(
        base_url=config.sandbox.base_url,
        headers={"X-API-Key": "fixture-key"},
        transport=httpx.MockTransport(server),
    )
    resources = CubeResources(config, "fixture-key", client=client)
    try:
        assert await resources.ready() is (enforced and valid_key)
    finally:
        await resources.close()
