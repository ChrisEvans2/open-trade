import httpx
import pytest
from trading_core.config import OKXConfig
from trading_core.market_data import okx_client as okx_client_module
from trading_core.market_data.okx_client import OKXClientError, OKXReadOnlyClient


def test_okx_extract_total_equity():
    data = {"data": [{"totalEq": "100.25", "details": []}]}
    assert OKXReadOnlyClient.extract_total_equity(data) == 100.25


@pytest.mark.asyncio
async def test_okx_get_retries_transient_403_and_ignores_env_proxy(monkeypatch):
    calls = []
    responses = [
        httpx.Response(403, json={"code": "403", "msg": "edge forbidden"}),
        httpx.Response(
            200, json={"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}], "msg": ""}
        ),
    ]

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls.append({"kwargs": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls[-1].update(
                {"method": method, "url": url, "headers": headers, "content": content}
            )
            return responses.pop(0)

    monkeypatch.setattr(okx_client_module.httpx, "AsyncClient", FakeAsyncClient)
    client = OKXReadOnlyClient(OKXConfig(max_read_retries=2, retry_backoff_seconds=0))
    ticker = await client.get_ticker("BTC-USDT-SWAP")
    assert ticker == {"instId": "BTC-USDT-SWAP"}
    assert len(calls) == 2
    assert calls[0]["kwargs"]["trust_env"] is False
    assert (
        calls[0]["headers"]["User-Agent"]
        == "open-trade/0.1 (OKX REST client)"
    )


@pytest.mark.asyncio
async def test_okx_post_does_not_retry_to_avoid_duplicate_orders(monkeypatch):
    calls = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls.append({"kwargs": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls[-1].update({"method": method, "url": url, "content": content})
            return httpx.Response(403, json={"code": "403", "msg": "edge forbidden"})

    monkeypatch.setattr(okx_client_module.httpx, "AsyncClient", FakeAsyncClient)
    client = OKXReadOnlyClient(
        OKXConfig(
            api_key="fixture-key",
            api_secret="fixture-secret",
            api_passphrase="fixture-pass",
            max_read_retries=2,
            retry_backoff_seconds=0,
        )
    )
    with pytest.raises(OKXClientError):
        await client.place_algo_order({"instId": "BTC-USDT-SWAP"})
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
