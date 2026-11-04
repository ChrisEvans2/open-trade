"""Standard-library HTTP boundary; no proxy, redirects, arbitrary URL or credential logging."""

import json
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE = 2_097_152


class PluginError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code)


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def decode(raw):
    def reject(_):
        raise ValueError("nonfinite JSON")

    return json.loads(raw, parse_float=Decimal, parse_constant=reject, object_pairs_hook=_pairs)


def encode(value):
    def decimal(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise TypeError("unsupported JSON value")

    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=decimal)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise PluginError("API_REDIRECT_FORBIDDEN", "领域API不可重定向")


class Client:
    def __init__(self, api_url, token):
        parsed = urlsplit(api_url or "")
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path.rstrip("/") not in {"/v1", "/v2"}
            or parsed.query
            or parsed.fragment
        ):
            raise PluginError(
                "API_CONFIGURATION_INVALID", "领域API必须为本机127.0.0.1的/v1或/v2入口"
            )
        if (
            not token
            or len(token) > 256
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise PluginError("API_CREDENTIAL_MISSING", "当前Profile缺少对应领域API凭据")
        self.api_url, self.token = api_url.rstrip("/"), token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, route, payload=None, *, key=None, session=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        if session:
            headers["X-Hermes-Session"] = session
        body = None if payload is None else encode(payload).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.api_url + route, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise PluginError("API_RESPONSE_TOO_LARGE", "领域API响应超过限额")
            return decode(raw)
        except HTTPError as exc:
            # Do not interpolate URLs, headers, request bodies or remote exception messages.
            code = f"API_HTTP_{exc.code}"
            try:
                error = decode(exc.read(4096)).get("error", {})
                candidate = error.get("code", "") if isinstance(error, dict) else ""
                if (
                    candidate
                    and len(candidate) <= 80
                    and all(c.isupper() or c in "_0123456789" for c in candidate)
                ):
                    code = candidate
            except (ValueError, AttributeError):
                pass
            raise PluginError(code, "领域API拒绝请求；请检查任务状态与提交字段") from None
        except (URLError, TimeoutError, OSError):
            raise PluginError("API_UNAVAILABLE", "领域API暂不可达") from None
        except (ValueError, TypeError):
            raise PluginError("API_RESPONSE_INVALID", "领域API返回非预期JSON") from None


def result(operation):
    try:
        return encode(operation())
    except PluginError as exc:
        return encode({"error": {"code": exc.code, "message": exc.message}})
    except Exception:
        return encode({"error": {"code": "PLUGIN_OPERATION_FAILED", "message": "研究适配操作失败"}})


def schema(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }
