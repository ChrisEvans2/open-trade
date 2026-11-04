from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

import httpx

from trading_agent.config import FeatureConfig, MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import (
    INTERVAL_SECONDS,
    OKX_BARS,
    TIMEFRAMES,
    boundary,
    require_anchor,
    utc,
)
from trading_agent.market.models import Bar, FormingCandle

PUBLIC_PATHS = frozenset(
    {
        "/api/v5/market/candles",
        "/api/v5/market/history-candles",
        "/api/v5/market/history-trades",
        "/api/v5/public/time",
        "/api/v5/public/instruments",
        "/api/v5/public/open-interest",
        "/api/v5/public/funding-rate",
        "/api/v5/public/funding-rate-history",
        "/api/v5/rubik/stat/contracts/open-interest-history",
        "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
        "/api/v5/rubik/stat/taker-volume-contract",
    }
)


class OKXClient:
    """Public GET-only adapter with a shared rate-limit gate and one absolute deadline."""

    def __init__(
        self,
        config: MarketConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        feature_config: FeatureConfig | None = None,
    ):
        self.config = config
        self.feature_config = feature_config
        self.now = now or (lambda: datetime.now(UTC))
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(
                config.read_timeout_seconds, connect=config.connect_timeout_seconds
            ),
        )
        self._rate_lock = asyncio.Lock()
        self._blocked_until = 0.0
        self._next_request = 0.0

    async def close(self):
        await self.client.aclose()

    async def observations(
        self, deadline, *, inst_id=None, symbol=None, timeframes=TIMEFRAMES, anchor=None
    ):
        from trading_agent.market.observations import collect

        if inst_id is None:
            return await collect(self, deadline, timeframes=timeframes, anchor=anchor)
        scoped = copy.copy(self)
        scoped.config = MarketConfig.model_validate(
            {
                **self.config.model_dump(),
                "inst_id": inst_id,
                "symbol": symbol,
                "contract_type": "perpetual",
                "settlement_asset": "USDT",
            }
        )
        scoped.get = self.get  # Keep the shared rate-limit gate and HTTP connection pool.
        return await collect(scoped, deadline, timeframes=timeframes, anchor=anchor)

    def remaining(self, deadline: datetime) -> float:
        remaining = (utc(deadline) - utc(self.now())).total_seconds()
        if remaining <= 0:
            raise DomainError("MARKET_DEADLINE", "行情请求超出采集截止时间", 503)
        return remaining

    async def _wait(self, seconds: float, deadline: datetime):
        if seconds >= self.remaining(deadline):
            raise DomainError("MARKET_DEADLINE", "行情重试会超出采集截止时间", 503)
        await asyncio.sleep(max(0, seconds))

    async def _gate(self, deadline):
        async with self._rate_lock:
            delay = max(self._blocked_until, self._next_request) - time.monotonic()
            if delay > 0:
                await self._wait(delay, deadline)
            self.remaining(deadline)
            self._next_request = time.monotonic() + 0.12

    async def get(self, path: str, params: dict, deadline: datetime) -> list:
        if path not in PUBLIC_PATHS:
            raise DomainError("MARKET_PATH_FORBIDDEN", "只允许已定义的公共数据GET接口", 403)
        for attempt in range(self.config.retries + 1):
            await self._gate(deadline)
            retry_delay = 2**attempt
            try:
                async with asyncio.timeout(self.remaining(deadline)):
                    response = await self.client.get(path, params=params)
                if response.status_code == 429:
                    header = response.headers.get("Retry-After", "1")
                    try:
                        retry_delay = max(0.0, float(header))
                    except ValueError:
                        try:
                            retry_delay = max(
                                0.0, (parsedate_to_datetime(header) - self.now()).total_seconds()
                            )
                        except (TypeError, ValueError):
                            retry_delay = 1
                    self._blocked_until = max(self._blocked_until, time.monotonic() + retry_delay)
                elif response.status_code >= 500:
                    pass
                elif response.status_code != 200:
                    raise DomainError("MARKET_REJECTED", "公共行情接口拒绝请求", 503)
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise DomainError("MARKET_SCHEMA", "公共行情响应不是JSON", 503) from exc
                    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                        raise DomainError("MARKET_REJECTED", "公共行情接口返回业务错误", 503)
                    if not isinstance(payload.get("data"), list):
                        raise DomainError("MARKET_SCHEMA", "公共行情缺少data数组", 503)
                    return payload["data"]
            except (httpx.TransportError, TimeoutError):
                pass
            if attempt < self.config.retries:
                await self._wait(retry_delay, deadline)
        raise DomainError("MARKET_UNAVAILABLE", "公共行情暂不可用", 503)

    async def check_clock(self, deadline: datetime):
        before = self.now()
        data = await self.get("/api/v5/public/time", {}, deadline)
        after = self.now()
        try:
            server = datetime.fromtimestamp(int(data[0]["ts"]) / 1000, UTC)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise DomainError("MARKET_SCHEMA", "交易所时钟响应无效", 503) from exc
        midpoint = before + (after - before) / 2
        if abs((server - midpoint).total_seconds()) > self.config.clock_skew_seconds:
            raise DomainError("CLOCK_SKEW", "服务器时钟与行情源偏差超过5秒", 503)

    async def instrument(self, deadline: datetime, *, inst_id=None) -> dict:
        if inst_id is None:
            self.config.require_instrument()
        elif inst_id not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}:
            raise DomainError("INSTRUMENT_MISMATCH", "预测合约不在允许范围")
        kind = "SWAP" if inst_id or self.config.contract_type == "perpetual" else "FUTURES"
        selected = inst_id or self.config.inst_id
        settlement = "USDT" if inst_id else self.config.settlement_asset
        data = await self.get(
            "/api/v5/public/instruments",
            {"instType": kind, "instId": selected},
            deadline,
        )
        matched = [item for item in data if item.get("instId") == selected]
        if (
            len(matched) != 1
            or matched[0].get("settleCcy") != settlement
            or matched[0].get("instType") != kind
        ):
            raise DomainError("INSTRUMENT_MISMATCH", "合约标识或结算资产与配置不一致")
        if matched[0].get("state") != "live":
            raise DomainError("INSTRUMENT_UNAVAILABLE", "配置合约当前不可用", 503)
        return matched[0]

    def parse_bar(
        self, row: list, timeframe: str, available_at: datetime, *, inst_id: str | None = None
    ) -> Bar | None:
        if not isinstance(row, list) or len(row) != 9:
            raise DomainError("MARKET_SCHEMA", "OKX K线必须为9字段数组", 503)
        if row[8] not in {"0", "1"}:
            raise DomainError("MARKET_SCHEMA", "K线confirm字段错误", 503)
        if row[8] != "1":
            return None
        try:
            open_at = datetime.fromtimestamp(int(row[0]) / 1000, UTC)
            numbers = [Decimal(str(value)) for value in row[1:8]]
            return Bar(
                inst_id=inst_id or self.config.inst_id,
                timeframe=timeframe,
                open_at=open_at,
                close_boundary=open_at + timedelta(seconds=INTERVAL_SECONDS[timeframe]),
                open=numbers[0],
                high=numbers[1],
                low=numbers[2],
                close=numbers[3],
                volume_contracts=numbers[4],
                volume_base=numbers[5],
                turnover_quote=numbers[6],
                available_at=available_at,
            )
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise DomainError("INVALID_BAR", "行情K线字段或OHLC校验失败", 503) from exc

    async def candles(
        self, anchor: datetime, timeframe: str, deadline: datetime, *, inst_id=None
    ) -> tuple[Bar, ...]:
        if inst_id is None:
            self.config.require_instrument()
        anchor = require_anchor(anchor)
        end = boundary(anchor, timeframe)
        data = await self.get(
            "/api/v5/market/history-candles",
            {
                "instId": inst_id or self.config.inst_id,
                "bar": OKX_BARS[timeframe],
                "after": str(int(end.timestamp() * 1000)),
                "limit": "100",
            },
            deadline,
        )
        available_at = self.now()
        bars = [
            bar
            for row in data
            if (bar := self.parse_bar(row, timeframe, available_at, inst_id=inst_id)) is not None
            and bar.close_boundary <= end
        ]
        bars.sort(key=lambda b: b.open_at)
        if len({bar.open_at for bar in bars}) != len(bars):
            raise DomainError("DUPLICATE_BAR", "行情源返回重复K线")
        return tuple(bars[-80:])

    async def forming_candle(
        self, timeframe: str, deadline: datetime, *, inst_id: str
    ) -> FormingCandle | None:
        """Fetch current OHLCV once, separately from the 80 confirmed candles."""
        if inst_id not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"} or timeframe not in {
            "1h",
            "4h",
            "6h",
            "12h",
        }:
            raise DomainError("INPUT_SCOPE", "未收盘快照的标的或周期不在预测范围")
        requested_at = utc(self.now())
        data = await self.get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": OKX_BARS[timeframe], "limit": "2"},
            deadline,
        )
        available_at = utc(self.now())
        start = boundary(available_at, timeframe)
        if boundary(requested_at, timeframe) != start:
            raise DomainError("FORMING_CANDLE_BOUNDARY", "采集未收盘快照时跨越周期边界", 503)
        selected = []
        try:
            for row in data:
                if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
                    raise ValueError("未收盘快照响应格式无效")
                if int(row[0]) == int(start.timestamp() * 1000) and row[8] == "0":
                    selected.append(row)
            if not selected:
                return None
            if len(selected) != 1:
                raise ValueError("当前未收盘K线重复")
            numbers = [Decimal(str(value)) for value in selected[0][1:8]]
            return FormingCandle(
                inst_id=inst_id,
                timeframe=timeframe,
                open_at=start,
                close_boundary=start + timedelta(seconds=INTERVAL_SECONDS[timeframe]),
                open=numbers[0],
                high=numbers[1],
                low=numbers[2],
                close=numbers[3],
                volume_contracts=numbers[4],
                volume_base=numbers[5],
                turnover_quote=numbers[6],
                requested_at=requested_at,
                available_at=available_at,
            )
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise DomainError("INVALID_FORMING_CANDLE", "未收盘K线字段或OHLC校验失败", 503) from exc

    async def minute_window(self, inst_id, start, end, deadline, *, max_pages=3):
        """Catch up oldest missing minutes first; bounded work never skips an early gap."""
        if inst_id not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}:
            raise DomainError("INSTRUMENT_MISMATCH", "分钟验证标的不在预测范围")
        start, end = utc(start), min(utc(end), boundary(self.now(), "1m"))
        if start.timestamp() % 60 or end.timestamp() % 60 or max_pages not in range(1, 11):
            raise DomainError("MINUTE_WINDOW", "分钟范围或分页预算无效")
        bars, cursor = {}, start
        for _ in range(max_pages):
            if cursor >= end:
                break
            page_end = min(cursor + timedelta(minutes=300), end)
            data = await self.get(
                "/api/v5/market/history-candles",
                {
                    "instId": inst_id,
                    "bar": "1m",
                    "after": str(int(page_end.timestamp() * 1000)),
                    "limit": "300",
                },
                deadline,
            )
            available_at = self.now()
            page = [
                bar
                for row in data
                if (bar := self.parse_bar(row, "1m", available_at, inst_id=inst_id)) is not None
                and cursor <= bar.open_at < page_end
            ]
            if len({b.open_at for b in page}) != len(page):
                raise DomainError("DUPLICATE_BAR", "分钟行情存在重复K线")
            for bar in page:
                bars[bar.open_at] = bar
            while cursor in bars:
                cursor = bars[cursor].close_boundary
            if cursor < page_end:
                break  # Missing data is not a license to advance the durable cursor.
        return tuple(bars[at] for at in sorted(bars))

    async def pricing_bar(
        self, open_at: datetime, deadline: datetime, *, inst_id: str | None = None
    ) -> Bar | None:
        self.config.require_instrument()
        open_at = utc(open_at)
        if open_at.second or open_at.microsecond:
            raise DomainError("INVALID_PRICING_TIME", "端点必须为整分钟")
        if self.now() < open_at + timedelta(minutes=1):
            return None
        data = await self.get(
            "/api/v5/market/history-candles",
            {
                "instId": inst_id or self.config.inst_id,
                "bar": "1m",
                "after": str(int((open_at + timedelta(minutes=1)).timestamp() * 1000)),
                "limit": "2",
            },
            deadline,
        )
        available_at = self.now()
        matched = [
            bar
            for row in data
            if (bar := self.parse_bar(row, "1m", available_at, inst_id=inst_id)) is not None
            and bar.open_at == open_at
        ]
        if len(matched) > 1:
            raise DomainError("DUPLICATE_BAR", "端点分钟K线重复")
        return matched[0] if matched else None
