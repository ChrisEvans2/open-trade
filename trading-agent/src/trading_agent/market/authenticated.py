"""Host-only user credentials and a GET-only OKX transport, independent of execution."""

from __future__ import annotations

import asyncio
import io
import os
import stat
from pathlib import Path

import httpx
from dotenv import dotenv_values
from trading_core.config import OKXConfig
from trading_core.market_data.okx_client import OKXReadOnlyClient

from trading_agent.domain.errors import DomainError
from trading_agent.market.okx import PUBLIC_PATHS, OKXClient

ACCOUNT_CONFIG = "/api/v5/account/config"
READ_PATHS = PUBLIC_PATHS | {ACCOUNT_CONFIG}
FIELDS = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")


def load_credentials(path: Path) -> OKXConfig:
    """Read only this explicit file; never export its values into process environment."""
    try:
        if not path.is_absolute():
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65_536:
                raise ValueError
            values = dotenv_values(stream=io.StringIO(handle.read()), interpolate=False)
        if not all(
            isinstance(values.get(name), str)
            and values[name]
            and values[name].isascii()
            and values[name] == values[name].strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in values[name])
            for name in FIELDS
        ):
            raise ValueError
        simulated = values.get("OKX_SIMULATED_TRADING", "0")
        if simulated not in {"0", "1", "false", "true", "False", "True"}:
            raise ValueError
        return OKXConfig(
            api_key=values[FIELDS[0]],
            api_secret=values[FIELDS[1]],
            api_passphrase=values[FIELDS[2]],
            simulated_trading=simulated in {"1", "true", "True"},
            trust_env_proxy=False,
        )
    except (OSError, ValueError, UnicodeError):
        raise DomainError(
            "OKX_CREDENTIAL_FILE_INVALID", "OKX 凭据文件缺失、权限不安全或格式无效", 503
        ) from None


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    """Check the final request before signing, even if a caller bypasses OKXClient.get."""

    def __init__(self, credentials, *, transport=None):
        self._signer = OKXReadOnlyClient(credentials)
        self._transport = transport or httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request):
        url = request.url
        if (
            request.method != "GET"
            or url.scheme != "https"
            or url.host != "www.okx.com"
            or url.port not in {None, 443}
            or url.userinfo
            or url.path not in READ_PATHS
            or request.content
            or url.fragment
        ):
            raise DomainError("OKX_READ_ONLY_FORBIDDEN", "只允许指定 OKX HTTPS GET 读取", 403)
        path = url.raw_path.decode("ascii")  # Includes the exact encoded query used on the wire.
        request.headers.update(self._signer._auth_headers("GET", path, ""))
        return await self._transport.handle_async_request(request)

    async def aclose(self):
        await self._transport.aclose()


class AuthenticatedOKXClient(OKXClient):
    def __init__(self, config, *, feature_config=None, transport=None):
        credentials = load_credentials(config.credentials_file)
        super().__init__(
            config,
            feature_config=feature_config,
            transport=ReadOnlyTransport(credentials, transport=transport),
        )

    async def check_clock(self, deadline):
        await super().check_clock(deadline)
        return await self.verify_read_access(deadline)

    async def verify_read_access(self, deadline):
        await self._gate(deadline)
        try:
            async with asyncio.timeout(self.remaining(deadline)):
                response = await self.client.get(ACCOUNT_CONFIG)
            if response.status_code != 200:
                raise ValueError
            payload = response.json()
            rows = payload.get("data")
            if str(payload.get("code")) != "0" or not isinstance(rows, list) or not rows:
                raise ValueError
            permissions = {p.strip() for p in rows[0]["perm"].split(",") if p.strip()}
            if "read_only" not in permissions:
                raise ValueError
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            raise DomainError(
                "OKX_READ_AUTH_UNVERIFIED", "未能确认 OKX 凭据的读取权限", 503
            ) from None
        # Persist permission facts only, never account identifiers or raw private responses.
        return {
            "account_read_verified": True,
            "credential_read_only": permissions == {"read_only"},
            "known_permissions": sorted(permissions & {"read_only", "trade", "withdraw"}),
            "unknown_permissions_present": bool(permissions - {"read_only", "trade", "withdraw"}),
            "transport_policy": "GET_ONLY",
        }


def create_market_client(config, *, feature_config=None):
    cls = AuthenticatedOKXClient if config.credentials_file is not None else OKXClient
    return cls(config, feature_config=feature_config)
