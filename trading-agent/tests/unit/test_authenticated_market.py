import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trading_agent.config import MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.market.authenticated import (
    AuthenticatedOKXClient,
    create_market_client,
    load_credentials,
)
from trading_agent.market.okx import OKXClient


@pytest.fixture
def credential_file(tmp_path):
    path = tmp_path / "operator.env"
    path.write_text(
        "OKX_API_KEY=fixture-key\nOKX_API_SECRET=fixture-secret\n"
        "OKX_API_PASSPHRASE='fixture-${NOT_EXPANDED}'\n"
    )
    path.chmod(0o600)
    return path


def test_explicit_file_does_not_expand_or_export_credentials(credential_file, monkeypatch):
    monkeypatch.setenv("NOT_EXPANDED", "ambient-value")
    monkeypatch.delenv("OKX_API_KEY", raising=False)
    credentials = load_credentials(credential_file)
    assert credentials.api_passphrase == "fixture-${NOT_EXPANDED}"
    import os

    assert "OKX_API_KEY" not in os.environ


@pytest.mark.parametrize("invalid", ["public_permissions", "symlink", "missing", "unicode"])
def test_unsafe_or_invalid_file_is_refused(credential_file, tmp_path, invalid):
    path = credential_file
    if invalid == "public_permissions":
        path.chmod(0o644)
    elif invalid == "symlink":
        path = tmp_path / "linked.env"
        path.symlink_to(credential_file)
    elif invalid == "missing":
        path.write_text("OKX_API_KEY=fixture-key\n")
    else:
        path.write_text(path.read_text().replace("fixture-${NOT_EXPANDED}", "中文"))
    with pytest.raises(DomainError) as caught:
        load_credentials(path)
    assert caught.value.code == "OKX_CREDENTIAL_FILE_INVALID"
    assert "fixture-key" not in str(caught.value)


async def test_signed_wire_query_and_private_permission_summary(credential_file):
    calls = []

    def server(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["OK-ACCESS-KEY"] == "fixture-key"
        assert request.headers["OK-ACCESS-PASSPHRASE"] == "fixture-${NOT_EXPANDED}"
        payload = request.headers["OK-ACCESS-TIMESTAMP"] + "GET" + request.url.raw_path.decode()
        expected = base64.b64encode(
            hmac.new(b"fixture-secret", payload.encode(), hashlib.sha256).digest()
        ).decode()
        assert request.headers["OK-ACCESS-SIGN"] == expected
        rows = [{"perm": "read_only,trade", "uid": "fixture-private-account"}]
        return httpx.Response(200, json={"code": "0", "data": rows})

    client = AuthenticatedOKXClient(
        MarketConfig(credentials_file=credential_file), transport=httpx.MockTransport(server)
    )
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        summary = await client.verify_read_access(deadline)
        assert summary["account_read_verified"]
        assert not summary["credential_read_only"]
        assert summary["transport_policy"] == "GET_ONLY"
        assert "fixture-private-account" not in str(summary)
        await client.get(
            "/api/v5/market/history-candles", {"instId": "BTC-USDT-SWAP", "bar": "1H"}, deadline
        )
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("POST", "https://www.okx.com/api/v5/trade/order", None),
        ("POST", "https://www.okx.com/api/v5/account/config", None),
        ("GET", "https://www.okx.com/api/v5/trade/order", None),
        ("GET", "https://untrusted.invalid/api/v5/account/config", None),
        ("GET", "http://www.okx.com/api/v5/account/config", None),
        ("GET", "https://www.okx.com:8443/api/v5/account/config", None),
        ("GET", "https://www.okx.com/api/v5/account/config", b"forbidden-body"),
    ],
)
async def test_transport_rejects_writes_and_untrusted_destinations_before_io(
    credential_file, method, url, body
):
    def forbidden(request):
        pytest.fail("A rejected request must not reach the network")

    client = AuthenticatedOKXClient(
        MarketConfig(credentials_file=credential_file), transport=httpx.MockTransport(forbidden)
    )
    try:
        with pytest.raises(DomainError) as caught:
            await client.client.request(method, url, content=body)
        assert caught.value.code == "OKX_READ_ONLY_FORBIDDEN"
    finally:
        await client.close()


async def test_no_redirect_and_missing_file_never_falls_back(credential_file):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.invalid/"})

    client = AuthenticatedOKXClient(
        MarketConfig(credentials_file=credential_file), transport=httpx.MockTransport(server)
    )
    try:
        with pytest.raises(DomainError) as caught:
            await client.verify_read_access(datetime.now(UTC) + timedelta(seconds=5))
        assert caught.value.code == "OKX_READ_AUTH_UNVERIFIED"
        assert len(calls) == 1
    finally:
        await client.close()
    credential_file.unlink()
    with pytest.raises(DomainError):
        create_market_client(MarketConfig(credentials_file=credential_file))


async def test_public_mode_is_retained_when_no_file_is_configured():
    client = create_market_client(MarketConfig())
    try:
        assert type(client) is OKXClient
    finally:
        await client.close()


async def test_standard_collection_preflight_authenticates_private_read(credential_file):
    calls = []

    def server(request):
        calls.append(request.url.path)
        rows = (
            [{"ts": str(int(datetime.now(UTC).timestamp() * 1000))}]
            if request.url.path.endswith("/time")
            else [{"perm": "read_only"}]
        )
        return httpx.Response(200, json={"code": "0", "data": rows})

    client = AuthenticatedOKXClient(
        MarketConfig(credentials_file=credential_file), transport=httpx.MockTransport(server)
    )
    try:
        result = await client.check_clock(datetime.now(UTC) + timedelta(seconds=5))
        assert result["credential_read_only"]
        assert calls == ["/api/v5/public/time", "/api/v5/account/config"]
    finally:
        await client.close()
