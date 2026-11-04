from datetime import datetime, timezone

from trading_core.schemas import SocialItem
from trading_core.social.filter import SocialFilter


def item(text: str, asset: str | None = None) -> SocialItem:
    return SocialItem(
        source="BlockBeats Pro Newsflash",
        source_type="crypto_media",
        time=datetime.now(timezone.utc),
        asset=asset,
        text=text,
        source_trust=0.86,
    )


def test_filter_drops_unrelated_assetless_news_for_symbol():
    result = SocialFilter().filter_items(
        [
            item("Codex will change default models next week"),
            item("Semiconductor stocks hit a record weight in the S&P 500"),
            item("BTC ETF records large net outflows"),
            item("SEC announces new crypto market enforcement action"),
            item("SOL network upgrade completed", asset=None),
        ],
        symbol="SOLUSDT",
        lookback_hours=1,
    )

    texts = [row.text for row in result]
    assert "Codex will change default models next week" not in texts
    assert "Semiconductor stocks hit a record weight in the S&P 500" not in texts
    assert "BTC ETF records large net outflows" in texts
    assert "SEC announces new crypto market enforcement action" in texts
    assert "SOL network upgrade completed" in texts


def test_filter_keeps_explicit_asset_match():
    result = SocialFilter().filter_items(
        [item("Major incident reported", asset="DOGE")],
        symbol="DOGEUSDT",
        lookback_hours=1,
    )

    assert len(result) == 1
