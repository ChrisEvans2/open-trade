"""Standard-library HTTP boundary; no proxy, redirects, arbitrary URL or credential logging."""

import json
import os
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

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
    def __init__(self, api_url, token, analysis_run=None):
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
            raise PluginError("API_CONFIGURATION_INVALID", "领域API必须为本机127.0.0.1的版本化入口")
        if (
            not token
            or len(token) > 256
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise PluginError("API_CREDENTIAL_MISSING", "当前Profile缺少对应领域API凭据")
        self.api_url, self.token = api_url.rstrip("/"), token
        self.analysis_run = analysis_run
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @staticmethod
    def validation_hint(error):
        """Only schema field names and a fixed type vocabulary may leave the API."""
        fields = set()
        try:
            for name in ("prediction", "decision"):
                with (Path(os.environ["TA_INPUT_DIR"]) / f"{name}-schema.json").open("rb") as file:
                    raw = file.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    return ""
                schema = decode(raw)
                for item in (schema, *schema.get("$defs", {}).values()):
                    fields.update(item.get("properties", {}))
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            return ""
        kinds = {
            "missing",
            "extra_forbidden",
            "value_error",
            "literal_error",
            "uuid_parsing",
            "decimal_parsing",
            "greater_than",
            "less_than",
            "greater_than_equal",
            "less_than_equal",
            "list_type",
            "dict_type",
            "model_type",
            "string_type",
            "int_type",
            "bool_type",
            "finite_number",
            "too_long",
            "too_short",
            "string_too_long",
            "string_too_short",
            "decimal_max_digits",
            "decimal_max_places",
            "type_error",
        }
        hints = []
        issues = error.get("issues", [])
        if not isinstance(issues, list):
            return ""
        for issue in issues[:8]:
            if not isinstance(issue, dict) or issue.get("type") not in kinds:
                continue
            path = issue.get("path")
            if (
                not isinstance(path, list)
                or len(path) > 12
                or not all(
                    (isinstance(part, str) and part in fields)
                    or (type(part) is int and 0 <= part <= 100)
                    for part in path
                )
            ):
                continue
            hints.append((".".join(map(str, path)) or "payload") + ":" + issue["type"])
        return "; 错误字段=" + ", ".join(hints) if hints else ""

    def request(self, method, route, payload=None, *, key=None, session=None, timeout=10, run=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if bound := (run or self.analysis_run):
            headers["X-Analysis-Run"] = str(bound)
        if key:
            headers["Idempotency-Key"] = key
        if session:
            headers["X-Hermes-Session"] = session
        body = None if payload is None else encode(payload).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.api_url + route, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise PluginError("API_RESPONSE_TOO_LARGE", "领域API响应超过限额")
            return decode(raw)
        except HTTPError as exc:
            # Do not interpolate URLs, headers, request bodies or remote exception messages.
            code = f"API_HTTP_{exc.code}"
            error = {}
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
            hints = {
                "REQUEST_INVALID": (
                    "请按/workspace/input中的任务schema检查payload；evidence_ids、"
                    "knowledge_version_ids、child_prediction_ids各数组内不可重复，"
                    "概率和须为1，方向与情景须一致，不得添加额外字段"
                ),
                "EVIDENCE_REFERENCE": (
                    "prediction.evidence_ids仅引用本次input_id、bar_ids、observation_ids；"
                    "子预测ID放child_prediction_ids，知识版本ID放knowledge_version_ids"
                ),
                "BOUNDARY_REFERENCE": (
                    "复制冻结候选的line和tolerance；UNKNOWN复制fallback_lines及"
                    "fallback_tolerance字符串，不用float重算或四舍五入"
                ),
                "PREDICTION_GEOMETRY": (
                    "UP情景target.low须高于发布时触发线价格；DOWN情景target.high须低于"
                    "触发线价格；所有条件线在整个期限内保持正数，不可改冻结边界来适配目标"
                ),
                "CONTEXT_CHANGED": "冻结上下文已变化，停止提交旧结果，由runtime重新调度",
                "CONTEXT_EXPIRED": "冻结输入已过期，停止提交旧结果，由runtime创建新任务",
                "MINUTE_DATA_PENDING": (
                    "分钟行情核验尚未补齐；可在本attempt原有时限内稍后再提交。"
                    "若实际上下文已变化则停止旧提交，由runtime刷新"
                ),
            }
            message = hints.get(code, "领域API拒绝请求；请检查任务状态与提交字段")
            if code == "REQUEST_INVALID" and isinstance(error, dict):
                message += self.validation_hint(error)
            if code in {"REQUEST_INVALID", "EVIDENCE_REFERENCE"} and route in {
                "/research/decision",
                "/research/prediction",
            }:
                try:
                    # Read only the runtime-owned frozen input. Never echo the
                    # rejected body, remote error text or unrelated context.
                    with (Path(os.environ["TA_INPUT_DIR"]) / "context.json").open("rb") as file:
                        frozen = file.read(MAX_RESPONSE + 1)
                    if len(frozen) > MAX_RESPONSE:
                        raise ValueError("oversized input")
                    ident = str(UUID(str(decode(frozen)["input_id"])))
                    message += (
                        '; evidence_ids可仅填写本次input_id：["'
                        + ident
                        + '"]。无需枚举bar_ids；不要复制children中的snapshot_id/input_id'
                    )
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            raise PluginError(code, message) from None
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
