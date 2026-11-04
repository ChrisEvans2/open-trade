from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from trading_core.config import OKXConfig


class OKXClientError(RuntimeError):
    def __init__(self, message: str, response: dict[str, Any] | None = None):
        super().__init__(message)
        self.response = response or {}


class OKXReadOnlyClient:
    def __init__(self, config: OKXConfig):
        self.config = config

    async def get_candles(
        self, inst_id: str, bar: str, limit: int = 100
    ) -> list[list[str]]:
        response = await self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))},
            auth=False,
        )
        return response.get("data", [])

    async def get_history_candles(
        self, inst_id: str, bar: str, limit: int = 100
    ) -> list[list[str]]:
        response = await self._request(
            "GET",
            "/api/v5/market/history-candles",
            params={"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))},
            auth=False,
        )
        return response.get("data", [])

    async def get_ticker(self, inst_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            "/api/v5/market/ticker",
            params={"instId": inst_id},
            auth=False,
        )
        rows = response.get("data") or []
        return rows[0] if rows else None

    async def get_tickers(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        response = await self._request(
            "GET", "/api/v5/market/tickers", params={"instType": inst_type}, auth=False
        )
        return response.get("data", [])

    async def get_books(self, inst_id: str, size: int = 5) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            "/api/v5/market/books",
            params={"instId": inst_id, "sz": str(min(max(size, 1), 50))},
            auth=False,
        )
        rows = response.get("data") or []
        return rows[0] if rows else None

    async def get_funding_rate(self, inst_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            "/api/v5/public/funding-rate",
            params={"instId": inst_id},
            auth=False,
        )
        rows = response.get("data") or []
        return rows[0] if rows else None

    async def get_balance(self, ccy: str | None = None) -> dict[str, Any]:
        params = {"ccy": ccy} if ccy else None
        return await self._request(
            "GET", "/api/v5/account/balance", params=params, auth=True
        )

    async def get_account_equity(self) -> float | None:
        data = await self.get_balance()
        return self.extract_total_equity(data)

    async def get_funding_balance(self, ccy: str | None = None) -> dict[str, Any]:
        params = {"ccy": ccy} if ccy else None
        return await self._request(
            "GET", "/api/v5/asset/balances", params=params, auth=True
        )

    async def get_instruments(
        self, inst_type: str = "SWAP", inst_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        response = await self._request(
            "GET", "/api/v5/public/instruments", params=params, auth=False
        )
        return response.get("data", [])

    async def get_positions(
        self, inst_type: str = "SWAP", inst_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        response = await self._request(
            "GET", "/api/v5/account/positions", params=params, auth=True
        )
        return response.get("data", [])

    async def get_positions_history(
        self,
        inst_type: str = "SWAP",
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {"instType": inst_type, "limit": str(min(limit, 100))}
        if inst_id:
            params["instId"] = inst_id
        response = await self._request(
            "GET", "/api/v5/account/positions-history", params=params, auth=True
        )
        return response.get("data", [])

    async def get_pending_orders(
        self, inst_type: str = "SWAP", inst_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        response = await self._request(
            "GET", "/api/v5/trade/orders-pending", params=params, auth=True
        )
        return response.get("data", [])

    async def cancel_order(
        self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        return await self._request(
            "POST", "/api/v5/trade/cancel-order", body=body, auth=True
        )

    async def set_leverage(
        self, inst_id: str, lever: float, mgn_mode: str = "isolated"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode},
            auth=True,
        )

    async def place_order(
        self,
        inst_id: str,
        side: str,
        size: str,
        client_order_id: str,
        td_mode: str = "isolated",
        order_type: str = "market",
        reduce_only: bool = False,
        attach_algo_orders: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": order_type,
            "sz": size,
            "clOrdId": client_order_id,
        }
        if reduce_only:
            body["reduceOnly"] = "true"
        if attach_algo_orders:
            body["attachAlgoOrds"] = attach_algo_orders
        return await self._request("POST", "/api/v5/trade/order", body=body, auth=True)

    async def place_algo_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v5/trade/order-algo", body=body, auth=True
        )

    async def get_order(
        self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None
    ) -> dict[str, Any] | None:
        params: dict[str, str] = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        response = await self._request(
            "GET", "/api/v5/trade/order", params=params, auth=True
        )
        rows = response.get("data") or []
        return rows[0] if rows else None

    async def get_fills(
        self, inst_id: str, ord_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params = {"instType": "SWAP", "instId": inst_id, "limit": str(min(limit, 100))}
        if ord_id:
            params["ordId"] = ord_id
        response = await self._request(
            "GET", "/api/v5/trade/fills", params=params, auth=True
        )
        return response.get("data", [])

    async def get_fills_history(
        self, begin_ms: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params = {"instType": "SWAP", "limit": str(min(limit, 100))}
        if begin_ms is not None:
            params["begin"] = str(begin_ms)
        response = await self._request(
            "GET", "/api/v5/trade/fills-history", params=params, auth=True
        )
        return response.get("data", [])

    async def get_pending_algo_orders(
        self, ord_type: str, inst_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"ordType": ord_type}
        if inst_id:
            params["instId"] = inst_id
        response = await self._request(
            "GET", "/api/v5/trade/orders-algo-pending", params=params, auth=True
        )
        return response.get("data", [])

    async def cancel_algo_orders(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        body = [
            {"instId": str(row["instId"]), "algoId": str(row["algoId"])}
            for row in orders
            if row.get("instId") and row.get("algoId")
        ]
        if not body:
            return {"code": "0", "data": []}
        return await self._request(
            "POST", "/api/v5/trade/cancel-algos", body=body, auth=True
        )

    @staticmethod
    def extract_total_equity(data: dict[str, Any]) -> float | None:
        rows = data.get("data") or []
        if not rows:
            return None
        raw = rows[0].get("totalEq")
        try:
            equity = float(raw)
        except (TypeError, ValueError):
            return None
        return equity if equity > 0 else None

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.config.base_url.rstrip('/')}{path}{query}"
        headers: dict[str, str] = {"User-Agent": self.config.user_agent}
        content = json.dumps(body) if body else ""
        if auth:
            headers.update(self._auth_headers(method, path + query, content))
        attempts = max(0, self.config.max_read_retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=20, trust_env=self.config.trust_env_proxy
                ) as client:
                    response = await client.request(
                        method, url, headers=headers, content=content or None
                    )
                try:
                    data = response.json()
                except ValueError:
                    if self._should_retry(method, response.status_code, attempt):
                        await self._retry_sleep(attempt)
                        continue
                    detail = self._http_error_detail(response)
                    raise OKXClientError(
                        f"OKX HTTP {response.status_code} non-json response {detail}"
                    )
                if response.is_error:
                    if self._should_retry(method, response.status_code, attempt):
                        await self._retry_sleep(attempt)
                        continue
                    raise OKXClientError(
                        f"OKX HTTP {response.status_code} code={data.get('code')} msg={data.get('msg')}"
                        f" {self._http_error_detail(response)}",
                        data,
                    )
                if data.get("code") != "0":
                    raise OKXClientError(
                        f"OKX API error code={data.get('code')} msg={data.get('msg')}",
                        data,
                    )
                return data
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if self._should_retry(method, None, attempt):
                    await self._retry_sleep(attempt)
                    continue
                raise
        if last_error:
            raise last_error
        raise OKXClientError(f"OKX request failed after {attempts} attempts")

    def _should_retry(self, method: str, status_code: int | None, attempt: int) -> bool:
        if method.upper() != "GET":
            return False
        if attempt >= max(0, self.config.max_read_retries):
            return False
        if status_code is None:
            return True
        return status_code in {403, 408, 425, 429} or status_code >= 500

    async def _retry_sleep(self, attempt: int) -> None:
        backoff = max(0.0, self.config.retry_backoff_seconds) * (attempt + 1)
        if backoff > 0:
            await asyncio.sleep(backoff)

    @staticmethod
    def _http_error_detail(response: httpx.Response) -> str:
        cf_ray = response.headers.get("cf-ray")
        content_type = response.headers.get("content-type")
        parts = []
        if content_type:
            parts.append(f"content_type={content_type}")
        if cf_ray:
            parts.append(f"cf_ray={cf_ray}")
        preview = response.text[:160].replace("\n", " ").strip()
        if preview:
            parts.append(f"body={preview!r}")
        return " ".join(parts)

    def _auth_headers(
        self, method: str, request_path: str, body: str
    ) -> dict[str, str]:
        if not (
            self.config.api_key
            and self.config.api_secret
            and self.config.api_passphrase
        ):
            raise OKXClientError(
                "OKX private read requires OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE"
            )
        timestamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.config.api_secret.encode(), prehash.encode(), hashlib.sha256
        ).digest()
        headers = {
            "OK-ACCESS-KEY": self.config.api_key,
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.config.api_passphrase,
            "Content-Type": "application/json",
        }
        if self.config.simulated_trading:
            headers["x-simulated-trading"] = "1"
        return headers
