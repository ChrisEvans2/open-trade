from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_core.schemas import MarketSnapshot, utc_now
from trading_core.market_data.okx_client import OKXReadOnlyClient


class MarketDataService:
    async def fetch_latest_closed_kline(self, symbol: str, timeframe: str) -> MarketSnapshot:
        # Placeholder collector for paper-mode framework validation.
        now = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=now,
            open=100_000,
            high=101_200,
            low=99_500,
            close=100_800,
            volume=12_345.67,
            funding_rate=0.0001,
            open_interest=123_456_789,
        )

    async def fetch_mark_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        return await self.fetch_latest_closed_kline(symbol, timeframe)


class OKXMarketDataService(MarketDataService):
    def __init__(self, client: OKXReadOnlyClient, candle_limit: int = 120):
        self.client = client
        self.candle_limit = candle_limit

    async def fetch_latest_closed_kline(self, symbol: str, timeframe: str) -> MarketSnapshot:
        candles = await self.fetch_recent_candles(symbol, timeframe, self.candle_limit)
        if not candles:
            raise ValueError(f"no closed OKX candles for {symbol} {timeframe}")
        latest = candles[-1]
        funding = await self._fetch_funding(symbol)
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=latest["timestamp"],
            open=latest["open"],
            high=latest["high"],
            low=latest["low"],
            close=latest["close"],
            volume=latest["volume"],
            funding_rate=funding.get("funding_rate"),
            next_funding_rate=funding.get("next_funding_rate"),
            funding_time=funding.get("funding_time"),
            funding_inst_id=funding.get("funding_inst_id"),
            open_interest=None,
        )

    async def fetch_recent_candles(self, symbol: str, timeframe: str, limit: int = 120) -> list[dict]:
        raw = await self.client.get_candles(self._inst_id(symbol), self._bar(timeframe), limit=limit)
        parsed = [self._parse_candle(row) for row in raw if self._is_closed(row)]
        parsed.sort(key=lambda row: row["timestamp"])
        return parsed

    async def fetch_mark_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        inst_id = self._inst_id(symbol)
        row = await self.client.get_ticker(inst_id)
        if not row:
            return await self.fetch_latest_closed_kline(symbol, timeframe)
        last = self._optional_float(row.get("last"))
        if last is None or last <= 0:
            return await self.fetch_latest_closed_kline(symbol, timeframe)
        funding = await self._fetch_funding(symbol)
        timestamp = self._optional_ms_datetime(row.get("ts")) or utc_now()
        volume = self._optional_float(row.get("vol24h")) or 0
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            open=last,
            high=last,
            low=last,
            close=last,
            volume=volume,
            funding_rate=funding.get("funding_rate"),
            next_funding_rate=funding.get("next_funding_rate"),
            funding_time=funding.get("funding_time"),
            funding_inst_id=funding.get("funding_inst_id"),
            open_interest=None,
        )

    @staticmethod
    def _inst_id(symbol: str) -> str:
        if "-" in symbol:
            return symbol
        if symbol.endswith("USDT"):
            return f"{symbol[:-4]}-USDT"
        return symbol

    @staticmethod
    def _swap_inst_id(symbol: str) -> str:
        inst_id = OKXMarketDataService._inst_id(symbol)
        return inst_id if inst_id.endswith("-SWAP") else f"{inst_id}-SWAP"

    @staticmethod
    def _bar(timeframe: str) -> str:
        mapping = {"15m": "15m", "1h": "1H", "2h": "2H", "4h": "4H", "12h": "12H", "1d": "1D"}
        return mapping.get(timeframe.lower(), timeframe)

    @staticmethod
    def _is_closed(row: list[str]) -> bool:
        return len(row) < 9 or row[8] == "1"

    @staticmethod
    def _parse_candle(row: list[str]) -> dict:
        return {
            "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }

    async def _fetch_funding(self, symbol: str) -> dict:
        inst_id = self._swap_inst_id(symbol)
        try:
            row = await self.client.get_funding_rate(inst_id)
        except Exception:
            return {"funding_inst_id": inst_id}
        if not row:
            return {"funding_inst_id": inst_id}
        return self._parse_funding_rate(row, inst_id)

    @staticmethod
    def _parse_funding_rate(row: dict, inst_id: str) -> dict:
        return {
            "funding_inst_id": inst_id,
            "funding_rate": OKXMarketDataService._optional_float(row.get("fundingRate")),
            "next_funding_rate": OKXMarketDataService._optional_float(row.get("nextFundingRate")),
            "funding_time": OKXMarketDataService._optional_ms_datetime(row.get("fundingTime")),
        }

    @staticmethod
    def _optional_float(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_ms_datetime(value) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
