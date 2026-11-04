from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from trading_core.schemas import SocialItem, utc_now


class SocialSource:
    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        raise NotImplementedError


class FixtureSource(SocialSource):
    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        asset = symbol.removesuffix("USDT")
        return [
            SocialItem(
                source="manual_fixture",
                source_type="fixture",
                time=utc_now(),
                asset=asset,
                text=f"{asset} market update: no major negative official event detected in fixture feed.",
                source_trust=0.5,
            )
        ]


class RSSSource(SocialSource):
    def __init__(
        self,
        name: str,
        url: str,
        source_type: str,
        trust: float,
        headers: dict[str, str] | None = None,
    ):
        self.name = name
        self.url = url
        self.source_type = source_type
        self.trust = trust
        self.headers = headers or {}

    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        headers = {"User-Agent": "ai-trading-system/0.1", **self.headers}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(self.url, headers=headers)
            response.raise_for_status()
        return self._parse_feed(response.text)

    def _parse_feed(self, text: str) -> list[SocialItem]:
        stripped = text.lstrip()
        if stripped.startswith("{"):
            return self._parse_json_feed(stripped)
        root = ET.fromstring(text)
        items: list[SocialItem] = []
        if root.tag.endswith("feed"):
            entries = root.findall(
                "{http://www.w3.org/2005/Atom}entry"
            ) or root.findall("entry")
            for entry in entries:
                title = self._find_text(
                    entry, ["{http://www.w3.org/2005/Atom}title", "title"]
                )
                summary = self._find_text(
                    entry,
                    [
                        "{http://www.w3.org/2005/Atom}summary",
                        "summary",
                        "{http://www.w3.org/2005/Atom}content",
                        "content",
                    ],
                )
                updated = self._find_text(
                    entry,
                    [
                        "{http://www.w3.org/2005/Atom}updated",
                        "updated",
                        "{http://www.w3.org/2005/Atom}published",
                        "published",
                    ],
                )
                link = self._atom_link(entry)
                items.append(self._item(title, summary, updated, link))
            return items

        channel = root.find("channel")
        rss_items = (
            channel.findall("item") if channel is not None else root.findall(".//item")
        )
        for item in rss_items:
            title = self._find_text(item, ["title"])
            summary = self._find_text(item, ["description", "summary"])
            published = self._find_text(item, ["pubDate", "published", "updated"])
            link = self._find_text(item, ["link"])
            items.append(self._item(title, summary, published, link))
        return items

    def _parse_json_feed(self, text: str) -> list[SocialItem]:
        data = json.loads(text)
        rows = data.get("data") or data.get("items") or []
        if not isinstance(rows, list):
            return []
        items: list[SocialItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or row.get("name") or "").strip()
            summary = str(
                row.get("description") or row.get("summary") or row.get("content") or ""
            ).strip()
            published = str(
                row.get("pubDate")
                or row.get("publish_time")
                or row.get("created_at")
                or ""
            )
            link = row.get("link") or row.get("url")
            items.append(self._item(title, summary, published, link))
        return items

    def _item(
        self, title: str, summary: str, published: str, link: str | None
    ) -> SocialItem:
        text = (
            " - ".join(
                part for part in [title, self._strip_markup(summary)] if part
            ).strip()
            or "untitled update"
        )
        return SocialItem(
            source=self.name,
            source_type=self.source_type,
            time=self._parse_time(published),
            asset=None,
            text=text[:5000],
            url=link,
            engagement=0,
            source_trust=self.trust,
        )

    @staticmethod
    def _find_text(element: ET.Element, names: list[str]) -> str:
        for name in names:
            found = element.find(name)
            if found is not None and found.text:
                return found.text.strip()
        return ""

    @staticmethod
    def _atom_link(element: ET.Element) -> str | None:
        for link in element.findall(
            "{http://www.w3.org/2005/Atom}link"
        ) + element.findall("link"):
            href = link.attrib.get("href")
            if href:
                return href
        return None

    @staticmethod
    def _parse_time(value: str) -> datetime:
        if not value:
            return utc_now()
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                for fmt in ("%b %d, %Y", "%B %d, %Y"):
                    try:
                        return datetime.strptime(value, fmt).replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        continue
                return utc_now()
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _strip_markup(value: str) -> str:
        return " ".join(value.replace("<![CDATA[", "").replace("]]>", "").split())


class GitHubReleasesSource(RSSSource):
    def __init__(self, name: str, repo: str, source_type: str, trust: float):
        super().__init__(
            name=name,
            url=f"https://github.com/{repo}/releases.atom",
            source_type=source_type,
            trust=trust,
        )


class BybitAnnouncementsSource(SocialSource):
    def __init__(self, name: str, locale: str, source_type: str, trust: float):
        self.name = name
        self.locale = locale
        self.source_type = source_type
        self.trust = trust

    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        params = {"locale": self.locale, "limit": "20"}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(
                    "https://api.bybit.com/v5/announcements/index", params=params
                )
                response.raise_for_status()
            data = response.json()
            if data.get("retCode") != 0:
                return []
            rows = data.get("result", {}).get("list", [])
        except httpx.HTTPError:
            return await self._fetch_from_page()
        items = []
        for row in rows:
            published = row.get("publishTime") or row.get("dateTimestamp") or 0
            items.append(
                SocialItem(
                    source=self.name,
                    source_type=self.source_type,
                    time=self._parse_ms(published),
                    asset=None,
                    text=(
                        row.get("title")
                        or row.get("description")
                        or "Bybit announcement"
                    )[:5000],
                    url=row.get("url"),
                    source_trust=self.trust,
                )
            )
        return items

    async def _fetch_from_page(self) -> list[SocialItem]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                "https://announcements.bybit.com/en/",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text,
        )
        if not match:
            return []
        import json

        data = json.loads(match.group(1))
        rows = (
            data.get("props", {})
            .get("pageProps", {})
            .get("articleInitEntity", {})
            .get("list", [])
        )
        items: list[SocialItem] = []
        for row in rows:
            path = row.get("url")
            url = (
                f"https://announcements.bybit.com/en{path}"
                if path and path.startswith("/")
                else path
            )
            items.append(
                SocialItem(
                    source=self.name,
                    source_type=self.source_type,
                    time=self._parse_ms(
                        row.get("publish_time") or row.get("date_timestamp")
                    ),
                    asset=None,
                    text=(
                        row.get("title")
                        or row.get("description")
                        or "Bybit announcement"
                    )[:5000],
                    url=url,
                    source_trust=self.trust,
                )
            )
        return items

    @staticmethod
    def _parse_ms(value: Any) -> datetime:
        try:
            number = int(value)
            if number > 10_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return utc_now()


class TelegramBotUpdatesSource(SocialSource):
    def __init__(self, channels: list[str], token_env: str, trust: float):
        self.channels = {channel.lstrip("@").lower() for channel in channels}
        self.token_env = token_env
        self.trust = trust

    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        token = os.getenv(self.token_env, "")
        if not token or not self.channels:
            return []
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"limit": "100"},
            )
            response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return []
        items: list[SocialItem] = []
        for update in data.get("result", []):
            message = update.get("channel_post") or update.get("message") or {}
            chat = message.get("chat", {})
            username = str(chat.get("username") or "").lower()
            if username and username not in self.channels:
                continue
            text = message.get("text") or message.get("caption") or ""
            if not text:
                continue
            items.append(
                SocialItem(
                    source=f"Telegram @{username or chat.get('id')}",
                    source_type="telegram_official_channel",
                    time=datetime.fromtimestamp(
                        int(message.get("date", 0)), tz=timezone.utc
                    ),
                    text=text,
                    url=None,
                    source_trust=self.trust,
                )
            )
        return items


class BlockBeatsProSource(SocialSource):
    base_url = "https://api-pro.theblockbeats.info"

    def __init__(
        self,
        name: str,
        endpoint: str,
        source_type: str,
        trust: float,
        api_key_env: str = "BLOCKBEATS_API_KEY",
        fallback_api_key_env: str = "BLOCKBEAT_API_KEY",
        lang: str = "cn",
        size: int = 10,
    ):
        self.name = name
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.source_type = source_type
        self.trust = trust
        self.api_key_env = api_key_env
        self.fallback_api_key_env = fallback_api_key_env
        self.lang = lang
        self.size = size

    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        api_key = os.getenv(self.api_key_env) or os.getenv(
            self.fallback_api_key_env, ""
        )
        if not api_key:
            print(
                f"{self.name} disabled: missing {self.api_key_env} or {self.fallback_api_key_env}",
                file=sys.stderr,
                flush=True,
            )
            return []
        params = {"page": 1, "size": self.size, "lang": self.lang}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                f"{self.base_url}{self.endpoint}",
                headers={"api-key": api_key},
                params=params,
            )
            response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 0:
            return []
        rows = self._extract_rows(payload.get("data"))
        return self._rows_to_items(rows)

    @classmethod
    def _extract_rows(cls, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if not isinstance(data, dict):
            return []
        rows = data.get("data") or data.get("list") or data.get("items") or []
        return (
            [row for row in rows if isinstance(row, dict)]
            if isinstance(rows, list)
            else []
        )

    def _rows_to_items(self, rows: list[dict[str, Any]]) -> list[SocialItem]:
        items: list[SocialItem] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            content = RSSSource._strip_markup(
                self._strip_html(str(row.get("abstract") or row.get("content") or ""))
            )
            text = " - ".join(part for part in [title, content] if part).strip()
            if not text:
                continue
            items.append(
                SocialItem(
                    source=self.name,
                    source_type=self.source_type,
                    time=RSSSource._parse_time(
                        str(row.get("create_time") or row.get("time") or "")
                    ),
                    asset=None,
                    text=text[:5000],
                    url=row.get("link") or row.get("url"),
                    source_trust=self.trust,
                )
            )
        return items

    @staticmethod
    def _strip_html(value: str) -> str:
        return re.sub(r"<[^>]+>", " ", value)


class XApiSource(SocialSource):
    base_url = "https://api.x.com"

    def __init__(
        self,
        name: str,
        source_type: str,
        trust: float,
        bearer_token_env: str = "X_BEARER_TOKEN",
        usernames: list[str] | None = None,
        queries: list[str] | None = None,
        max_results: int = 10,
        exclude_retweets: bool = True,
        exclude_replies: bool = True,
    ):
        self.name = name
        self.source_type = source_type
        self.trust = trust
        self.bearer_token_env = bearer_token_env
        self.usernames = [
            username.lstrip("@") for username in usernames or [] if username
        ]
        self.queries = queries or []
        self.max_results = max(5, min(100, max_results))
        self.exclude_retweets = exclude_retweets
        self.exclude_replies = exclude_replies

    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        token = os.getenv(self.bearer_token_env, "")
        if not token:
            print(
                f"{self.name} disabled: missing {self.bearer_token_env}",
                file=sys.stderr,
                flush=True,
            )
            return []

        headers = {"Authorization": f"Bearer {token}"}
        items: list[SocialItem] = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for username in self.usernames:
                try:
                    user = await self._lookup_user(client, headers, username)
                    items.extend(await self._fetch_user_posts(client, headers, user))
                except httpx.HTTPError as exc:
                    print(
                        f"{self.name} @{username} fetch failed: {exc.__class__.__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            for query in self._render_queries(symbol):
                try:
                    items.extend(
                        await self._fetch_recent_search(client, headers, query)
                    )
                except httpx.HTTPError as exc:
                    print(
                        f"{self.name} search fetch failed: {exc.__class__.__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
        return items

    async def _lookup_user(
        self, client: httpx.AsyncClient, headers: dict[str, str], username: str
    ) -> dict[str, Any]:
        response = await client.get(
            f"{self.base_url}/2/users/by/username/{quote(username)}",
            headers=headers,
            params={"user.fields": "username,name,verified,public_metrics"},
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        return {
            "id": str(data.get("id") or ""),
            "username": str(data.get("username") or username),
            "name": str(data.get("name") or username),
        }

    async def _fetch_user_posts(
        self, client: httpx.AsyncClient, headers: dict[str, str], user: dict[str, str]
    ) -> list[SocialItem]:
        if not user.get("id"):
            return []
        params = self._tweet_params()
        response = await client.get(
            f"{self.base_url}/2/users/{user['id']}/tweets",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return self._rows_to_items(
            response.json().get("data") or [], {user["id"]: user}
        )

    async def _fetch_recent_search(
        self, client: httpx.AsyncClient, headers: dict[str, str], query: str
    ) -> list[SocialItem]:
        params = {**self._tweet_params(), "query": query}
        response = await client.get(
            f"{self.base_url}/2/tweets/search/recent", headers=headers, params=params
        )
        response.raise_for_status()
        payload = response.json()
        users = {
            str(user.get("id")): user
            for user in payload.get("includes", {}).get("users", [])
            if isinstance(user, dict)
        }
        return self._rows_to_items(payload.get("data") or [], users)

    def _tweet_params(self) -> dict[str, str | int]:
        return {
            "max_results": self.max_results,
            "tweet.fields": "created_at,public_metrics,lang,author_id",
            "expansions": "author_id",
            "user.fields": "username,name,verified,public_metrics",
        }

    def _render_queries(self, symbol: str) -> list[str]:
        asset = symbol.removesuffix("USDT")
        rendered = []
        for query in self.queries:
            value = query.format(symbol=symbol, asset=asset)
            if self.exclude_retweets and "-is:retweet" not in value:
                value = f"{value} -is:retweet"
            if self.exclude_replies and "-is:reply" not in value:
                value = f"{value} -is:reply"
            rendered.append(value)
        return rendered

    def _rows_to_items(
        self, rows: list[dict[str, Any]], users: dict[str, dict[str, Any]]
    ) -> list[SocialItem]:
        items: list[SocialItem] = []
        for row in rows:
            text = str(row.get("text") or "").strip()
            tweet_id = str(row.get("id") or "")
            if not text or not tweet_id:
                continue
            author = users.get(str(row.get("author_id")), {})
            username = str(author.get("username") or "unknown")
            metrics = (
                row.get("public_metrics")
                if isinstance(row.get("public_metrics"), dict)
                else {}
            )
            engagement = (
                int(metrics.get("like_count") or 0)
                + int(metrics.get("retweet_count") or 0)
                + int(metrics.get("reply_count") or 0)
            )
            items.append(
                SocialItem(
                    source=f"{self.name} @{username}",
                    source_type=self.source_type,
                    time=RSSSource._parse_time(str(row.get("created_at") or "")),
                    asset=None,
                    text=text[:5000],
                    url=f"https://x.com/{username}/status/{tweet_id}",
                    engagement=engagement,
                    source_trust=self.trust,
                )
            )
        return items


class OKXAnnouncementsApiSource(SocialSource):
    def __init__(
        self,
        name: str,
        url: str,
        source_type: str,
        trust: float,
        cache_ttl_minutes: int = 55,
    ):
        self.name = name
        self.url = url
        self.source_type = source_type
        self.trust = trust
        self.cache_ttl = timedelta(minutes=max(1, cache_ttl_minutes))
        self._cache_until: datetime | None = None
        self._cache_items: list[SocialItem] | None = None

    async def fetch(self, symbol: str, lookback_hours: int) -> list[SocialItem]:
        now = utc_now()
        if (
            self._cache_until
            and self._cache_items is not None
            and now < self._cache_until
        ):
            return list(self._cache_items)

        headers = {"User-Agent": "open-trade/0.1 (OKX REST client)"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(self.url, headers=headers, params={"page": "1"})
            response.raise_for_status()
        items = self._parse_response(response.json())
        self._cache_items = items
        self._cache_until = now + self.cache_ttl
        return list(items)

    def _parse_response(self, payload: dict[str, Any]) -> list[SocialItem]:
        rows: list[dict[str, Any]] = []
        for group in payload.get("data") or []:
            if isinstance(group, dict):
                rows.extend(
                    row for row in group.get("details") or [] if isinstance(row, dict)
                )
        items: list[SocialItem] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            items.append(
                SocialItem(
                    source=self.name,
                    source_type=self.source_type,
                    time=self._parse_ms(row.get("pTime") or row.get("businessPTime")),
                    asset=None,
                    text=title,
                    url=row.get("url"),
                    source_trust=self.trust,
                )
            )
        return items

    @staticmethod
    def _parse_ms(value: Any) -> datetime:
        try:
            number = int(value)
            if number > 10_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return utc_now()


def build_sources(config: dict[str, Any]) -> list[SocialSource]:
    sources: list[SocialSource] = []
    for raw in config.get("sources", []):
        if not raw.get("enabled", False):
            continue
        source_type = raw.get("type")
        if source_type == "fixture":
            sources.append(FixtureSource())
        elif source_type == "rss":
            if raw.get("url"):
                sources.append(
                    RSSSource(
                        raw["name"],
                        raw["url"],
                        raw.get("source_type", "rss"),
                        float(raw.get("trust", 0.7)),
                        headers={
                            str(k): str(v) for k, v in raw.get("headers", {}).items()
                        },
                    )
                )
        elif source_type == "github_releases":
            sources.append(
                GitHubReleasesSource(
                    raw["name"],
                    raw["repo"],
                    raw.get("source_type", "github_releases"),
                    float(raw.get("trust", 0.8)),
                )
            )
        elif source_type == "bybit_announcements":
            sources.append(
                BybitAnnouncementsSource(
                    raw["name"],
                    raw.get("locale", "en-US"),
                    raw.get("source_type", "official_exchange"),
                    float(raw.get("trust", 0.9)),
                )
            )
        elif source_type in {"okx_announcements_api", "okx_announcements_page"}:
            sources.append(
                OKXAnnouncementsApiSource(
                    raw["name"],
                    raw.get("url", "https://www.okx.com/api/v5/support/announcements"),
                    raw.get("source_type", "official_exchange"),
                    float(raw.get("trust", 0.9)),
                    cache_ttl_minutes=int(raw.get("cache_ttl_minutes", 55)),
                )
            )
        elif source_type == "blockbeats_pro":
            sources.append(
                BlockBeatsProSource(
                    raw["name"],
                    raw.get("endpoint", "/v1/newsflash"),
                    raw.get("source_type", "crypto_media"),
                    float(raw.get("trust", 0.85)),
                    api_key_env=raw.get("api_key_env", "BLOCKBEATS_API_KEY"),
                    fallback_api_key_env=raw.get(
                        "fallback_api_key_env", "BLOCKBEAT_API_KEY"
                    ),
                    lang=raw.get("lang", "cn"),
                    size=int(raw.get("size", 10)),
                )
            )
        elif source_type == "x_api":
            sources.append(
                XApiSource(
                    raw["name"],
                    raw.get("source_type", "x_social"),
                    float(raw.get("trust", 0.65)),
                    bearer_token_env=raw.get("bearer_token_env", "X_BEARER_TOKEN"),
                    usernames=list(raw.get("usernames", [])),
                    queries=list(raw.get("queries", [])),
                    max_results=int(raw.get("max_results", 10)),
                    exclude_retweets=bool(raw.get("exclude_retweets", True)),
                    exclude_replies=bool(raw.get("exclude_replies", True)),
                )
            )

    telegram = config.get("telegram", {})
    if telegram.get("enabled", False) and telegram.get("mode") == "bot_updates":
        sources.append(
            TelegramBotUpdatesSource(
                channels=list(telegram.get("channels", [])),
                token_env=telegram.get("bot_token_env", "SOCIAL_TELEGRAM_BOT_TOKEN"),
                trust=float(telegram.get("trust", 0.75)),
            )
        )
    return sources
