import httpx
import pytest

from trading_core.social import sources as sources_module
from trading_core.social.sources import OKXAnnouncementsApiSource


def test_okx_announcements_api_parse_response():
    source = OKXAnnouncementsApiSource(
        name="OKX Latest Announcements",
        url="https://www.okx.com/api/v5/support/announcements",
        source_type="official_exchange",
        trust=0.9,
    )

    items = source._parse_response(
        {
            "code": "0",
            "msg": "",
            "data": [
                {
                    "details": [
                        {
                            "annType": "announcements-trading-updates",
                            "title": "OKX Announcement on Optimizing Spot Market Order Execution Rules",
                            "url": "https://www.okx.com/help/spot-market-order-partial-matching-adjustment",
                            "pTime": "1782711018759",
                        }
                    ]
                }
            ],
        }
    )

    assert len(items) == 1
    assert items[0].source == "OKX Latest Announcements"
    assert items[0].source_type == "official_exchange"
    assert items[0].source_trust == 0.9
    assert items[0].text == "OKX Announcement on Optimizing Spot Market Order Execution Rules"
    assert items[0].url == "https://www.okx.com/help/spot-market-order-partial-matching-adjustment"


@pytest.mark.asyncio
async def test_okx_announcements_api_uses_cache(monkeypatch):
    calls = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append({"url": url, "headers": headers, "params": params})
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "code": "0",
                    "msg": "",
                    "data": [{"details": [{"title": "OKX API update", "url": "https://www.okx.com/help/a", "pTime": "1782711018759"}]}],
                },
            )

    monkeypatch.setattr(sources_module.httpx, "AsyncClient", FakeAsyncClient)
    source = OKXAnnouncementsApiSource(
        name="OKX Latest Announcements",
        url="https://www.okx.com/api/v5/support/announcements",
        source_type="official_exchange",
        trust=0.9,
        cache_ttl_minutes=55,
    )

    first = await source.fetch("BTCUSDT", 1)
    second = await source.fetch("ETHUSDT", 1)

    assert len(first) == 1
    assert second[0].text == "OKX API update"
    assert len(calls) == 1
    assert calls[0]["params"] == {"page": "1"}
    assert calls[0]["headers"]["User-Agent"] == "open-trade/0.1 (OKX REST client)"
