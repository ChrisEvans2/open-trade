from __future__ import annotations

import asyncio
import logging
import sys

from trading_core.schemas import SocialItem
from trading_core.social.sources import FixtureSource, SocialSource

logger = logging.getLogger(__name__)


class SocialMonitor:
    def __init__(self, sources: list[SocialSource] | None = None):
        self.sources = sources if sources is not None else [FixtureSource()]

    async def fetch_recent(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        results = await asyncio.gather(
            *(
                source.fetch(symbol=symbol, lookback_hours=lookback_hours)
                for source in self.sources
            ),
            return_exceptions=True,
        )
        items: list[SocialItem] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("social source fetch failed: %s", result)
                print(
                    f"social source fetch failed: {result.__class__.__name__}: {result}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            items.extend(result)
        return items
