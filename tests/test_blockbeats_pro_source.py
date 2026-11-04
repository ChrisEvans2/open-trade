from trading_core.social.sources import BlockBeatsProSource


def test_blockbeats_pro_rows_to_items_strip_html():
    source = BlockBeatsProSource(
        name="BlockBeats Pro Newsflash",
        endpoint="/v1/newsflash",
        source_type="crypto_media",
        trust=0.86,
    )

    items = source._rows_to_items(
        [
            {
                "title": "BTC ETF net outflow",
                "content": "<p>BlockBeats news, ETF saw net outflow.</p>",
                "create_time": "2026-05-27 15:24:01",
                "link": "https://m.theblockbeats.info/flash/1",
            }
        ]
    )

    assert len(items) == 1
    assert items[0].source == "BlockBeats Pro Newsflash"
    assert items[0].source_type == "crypto_media"
    assert items[0].source_trust == 0.86
    assert items[0].text == "BTC ETF net outflow - BlockBeats news, ETF saw net outflow."
    assert items[0].url == "https://m.theblockbeats.info/flash/1"
