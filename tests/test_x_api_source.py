from trading_core.social.sources import XApiSource


def test_x_api_rows_to_items_include_author_and_engagement():
    source = XApiSource(
        name="X Crypto Watchlist",
        source_type="x_social",
        trust=0.65,
    )

    items = source._rows_to_items(
        [
            {
                "id": "123",
                "author_id": "42",
                "created_at": "2026-06-08T10:00:00Z",
                "text": "BTC ETF inflow update",
                "public_metrics": {"like_count": 10, "retweet_count": 2, "reply_count": 1},
            }
        ],
        {"42": {"username": "crypto_source", "name": "Crypto Source"}},
    )

    assert len(items) == 1
    assert items[0].source == "X Crypto Watchlist @crypto_source"
    assert items[0].source_type == "x_social"
    assert items[0].text == "BTC ETF inflow update"
    assert items[0].engagement == 13
    assert items[0].url == "https://x.com/crypto_source/status/123"
    assert items[0].source_trust == 0.65


def test_x_api_query_templates_render_symbol_and_exclusions():
    source = XApiSource(
        name="X Crypto Watchlist",
        source_type="x_social",
        trust=0.65,
        queries=["({asset} OR {symbol}) crypto"],
    )

    queries = source._render_queries("BTCUSDT")

    assert queries == ["(BTC OR BTCUSDT) crypto -is:retweet -is:reply"]
