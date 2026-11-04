from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from trading_core.market_data.okx_client import OKXReadOnlyClient


@dataclass
class UniverseCandidate:
    symbol: str
    inst_id: str
    market_tier: str
    score: float
    quote_volume_24h: float
    book_depth_usdt: float
    spread_pct: float
    atr_pct: float
    volume_stability: float
    data_quality: float


class OKXLiveUniverseSelector:
    """Select a small, non-meme USDT-swap universe from measurable market quality."""

    def __init__(self, client: OKXReadOnlyClient, config: dict[str, Any], state_path: str | Path):
        self.client = client
        self.config = config
        self.state_path = Path(state_path)

    async def select(self, default_timeframe: str) -> list[tuple[str, str]]:
        core = [str(symbol).upper() for symbol in self.config.get("core_symbols", [])]
        tier1 = [str(symbol).upper() for symbol in self.config.get("tier1_major_symbols", self.config.get("tier1_symbols", []))]
        tier2 = [str(symbol).upper() for symbol in self.config.get("tier2_major_symbols", self.config.get("tier2_symbols", []))]
        excluded = {str(symbol).upper() for symbol in self.config.get("excluded_symbols", [])}
        configured_candidates = self.config.get("candidates")
        candidates = (
            [str(symbol).upper() for symbol in configured_candidates]
            if configured_candidates
            else tier1 + tier2
        )
        candidates = [symbol for symbol in candidates if symbol not in core]
        candidates = [symbol for symbol in candidates if symbol not in excluded]
        if not self.config.get("enabled", False):
            return [(symbol, default_timeframe) for symbol in core]

        tickers, instruments = await asyncio.gather(self.client.get_tickers("SWAP"), self.client.get_instruments("SWAP"))
        ticker_by_id = {str(row.get("instId")): row for row in tickers}
        instrument_by_id = {str(row.get("instId")): row for row in instruments if row.get("state") == "live"}
        scored = await asyncio.gather(
            *(self._evaluate(symbol, ticker_by_id, instrument_by_id) for symbol in candidates), return_exceptions=True
        )
        accepted = [item for item in scored if isinstance(item, UniverseCandidate)]
        accepted.sort(key=lambda item: item.score, reverse=True)
        max_symbols = max(len(core), int(self.config.get("max_symbols", 8)))
        selected = core + [item.symbol for item in accepted[: max_symbols - len(core)]]
        self._persist(selected, accepted)
        return [(symbol, default_timeframe) for symbol in selected]

    async def _evaluate(
        self,
        symbol: str,
        ticker_by_id: dict[str, dict[str, Any]],
        instrument_by_id: dict[str, dict[str, Any]],
    ) -> UniverseCandidate | None:
        inst_id = self._swap_inst_id(symbol)
        ticker = ticker_by_id.get(inst_id)
        instrument = instrument_by_id.get(inst_id)
        if not ticker or not instrument:
            return None
        book, candles = await asyncio.gather(
            self.client.get_books(inst_id, 5), self.client.get_candles(inst_id, "1H", 25)
        )
        metrics = self._metrics(ticker, instrument, book, candles)
        if metrics is None:
            return None
        quote_volume, depth, spread, atr_pct, stability, quality = metrics
        if quote_volume < float(self.config.get("min_quote_volume_24h", 5_000_000)):
            return None
        if depth < float(self.config.get("min_book_depth_usdt", 10_000)):
            return None
        if spread > float(self.config.get("max_spread_pct", 0.0015)):
            return None
        if not float(self.config.get("min_atr_pct", 0.001)) <= atr_pct <= float(self.config.get("max_atr_pct", 0.08)):
            return None
        # Scores are normalized against conservative caps; 24h volume is only 40% of the result.
        liquidity_score = min(1.0, quote_volume / float(self.config.get("liquidity_reference_usdt", 500_000_000)))
        depth_score = min(1.0, depth / float(self.config.get("depth_reference_usdt", 1_000_000)))
        spread_score = max(0.0, 1 - spread / float(self.config.get("max_spread_pct", 0.0015)))
        score = 0.40 * liquidity_score + 0.20 * depth_score + 0.15 * spread_score + 0.15 * stability + 0.10 * quality
        return UniverseCandidate(symbol, inst_id, self._market_tier(symbol), score, quote_volume, depth, spread, atr_pct, stability, quality)

    def _market_tier(self, symbol: str) -> str:
        core = {str(item).upper() for item in self.config.get("core_symbols", [])}
        tier1 = {str(item).upper() for item in self.config.get("tier1_major_symbols", self.config.get("tier1_symbols", []))}
        tier2 = {str(item).upper() for item in self.config.get("tier2_major_symbols", self.config.get("tier2_symbols", []))}
        excluded = {str(item).upper() for item in self.config.get("excluded_symbols", [])}
        if symbol in excluded:
            return "excluded"
        if symbol in core:
            return "core"
        if symbol in tier1:
            return "tier1_major"
        if symbol in tier2:
            return "tier2_major"
        return str(self.config.get("default_market_tier", "tier2_major"))

    @staticmethod
    def _metrics(
        ticker: dict[str, Any], instrument: dict[str, Any], book: dict[str, Any] | None, candles: list[list[str]]
    ) -> tuple[float, float, float, float, float, float] | None:
        if not book or len(candles) < 20:
            return None
        try:
            last = float(ticker["last"])
            ct_val = float(instrument["ctVal"])
            volume = float(ticker.get("vol24h") or 0) * ct_val * last
            bids, asks = book.get("bids") or [], book.get("asks") or []
            bid, ask = float(bids[0][0]), float(asks[0][0])
            spread = (ask - bid) / ((ask + bid) / 2)
            depth = sum(float(row[0]) * float(row[1]) * ct_val for row in bids + asks)
            closed = [row for row in candles if len(row) < 9 or row[8] == "1"]
            ranges = [(float(row[2]) - float(row[3])) / float(row[4]) for row in closed if float(row[4]) > 0]
            volumes = [float(row[5]) for row in closed if float(row[5]) > 0]
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
            return None
        if len(ranges) < 20 or len(volumes) < 20:
            return None
        atr_pct = mean(ranges[-20:])
        volume_cv = pstdev(volumes[-20:]) / mean(volumes[-20:])
        stability = max(0.0, 1 - min(1.0, volume_cv / 2))
        return volume, depth, spread, atr_pct, stability, 1.0

    def _persist(self, selected: list[str], candidates: list[UniverseCandidate]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tier_by_symbol = {symbol: self._market_tier(symbol) for symbol in selected}
        self.state_path.write_text(
            json.dumps(
                {
                    "selected": selected,
                    "selected_tiers": tier_by_symbol,
                    "core_symbols": [str(item).upper() for item in self.config.get("core_symbols", [])],
                    "tier1_major_symbols": [
                        str(item).upper()
                        for item in self.config.get("tier1_major_symbols", self.config.get("tier1_symbols", []))
                    ],
                    "tier2_major_symbols": [
                        str(item).upper()
                        for item in self.config.get("tier2_major_symbols", self.config.get("tier2_symbols", []))
                    ],
                    "excluded_symbols": [str(item).upper() for item in self.config.get("excluded_symbols", [])],
                    "ranked": [asdict(item) for item in candidates],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _swap_inst_id(symbol: str) -> str:
        return f"{symbol[:-4]}-USDT-SWAP" if symbol.endswith("USDT") else symbol
