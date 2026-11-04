from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_core.schemas import SocialItem


class SocialFilter:
    def filter_items(
        self, items: list[SocialItem], symbol: str, lookback_hours: int, limit: int = 50
    ) -> list[SocialItem]:
        asset = symbol.removesuffix("USDT").upper()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        seen: set[tuple[str, str]] = set()
        selected: list[SocialItem] = []
        for item in items:
            text = item.text.strip()
            key = (item.source, text[:240])
            item_time = (
                item.time
                if item.time.tzinfo
                else item.time.replace(tzinfo=timezone.utc)
            )
            if item_time < cutoff or key in seen:
                continue
            if item.asset and item.asset.upper() not in {asset, symbol.upper()}:
                continue
            if item.asset is None and not self._is_relevant_to_symbol_or_market(
                text, asset, symbol
            ):
                continue
            if self._looks_like_spam(text):
                continue
            seen.add(key)
            selected.append(item)
        selected.sort(
            key=lambda item: (item.source_trust, item.engagement, item.time),
            reverse=True,
        )
        return selected[:limit]

    @staticmethod
    def _is_relevant_to_symbol_or_market(text: str, asset: str, symbol: str) -> bool:
        lowered = text.lower()
        upper = text.upper()
        if asset in upper or symbol.upper() in upper:
            return True

        broad_market_terms = (
            "btc",
            "bitcoin",
            "比特币",
            "eth",
            "ethereum",
            "以太坊",
            "crypto",
            "cryptocurrency",
            "加密",
            "链上",
            "交易所",
            "stablecoin",
            "usdt",
            "usdc",
            "etf",
            "sec",
            "cftc",
            "监管",
            "hack",
            "exploit",
            "被盗",
            "攻击",
            "漏洞",
            "美联储",
            "fed",
            "利率",
            "降息",
            "加息",
            "cpi",
            "pce",
            "非农",
            "通胀",
        )
        return any(
            term in lowered or term.upper() in upper for term in broad_market_terms
        )

    @staticmethod
    def _looks_like_spam(text: str) -> bool:
        lowered = text.lower()
        spam_terms = (
            "airdrop claim",
            "guaranteed profit",
            "100x",
            "send funds",
            "private key",
        )
        return any(term in lowered for term in spam_terms)
