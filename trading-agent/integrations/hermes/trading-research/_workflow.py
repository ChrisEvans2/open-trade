import math
import threading
from pathlib import Path
from uuid import uuid4

from ._http import PluginError, decode, encode


class Workflow:
    """Native delegation for one structural timeframe; server facts are authoritative."""

    def __init__(self, client, input_dir):
        self.client, self.input_dir = client, input_dir
        self.lock = threading.Lock()
        self.sessions_lock = threading.Lock()
        self.pending = {}
        self.sessions = {}
        self.pending_runs = {}
        self.session_runs = {}
        self.parent_run = None
        self.lifecycle = None
        self.failed = False
        self.structural_result = None

    def child_started(self, *, child_goal="", child_session_id=None, **_):
        marker = child_goal.split("\n", 1)[0]
        with self.sessions_lock:
            if marker in self.pending and child_session_id:
                self.sessions[marker] = str(child_session_id)
                if marker in self.pending_runs:
                    self.session_runs[str(child_session_id)] = self.pending_runs[marker]

    def parent(self):
        from agent.subagent_lifecycle import get_active_subagent_parent

        parent = get_active_subagent_parent()
        if parent is None or getattr(parent, "_delegate_depth", 0) != 0:
            raise PluginError("PARENT_CONTEXT_REQUIRED", "周期分析只允许本次父Agent调用")
        return parent

    def context(self):
        parent = self.parent()
        return self.client().request("GET", "/research/context", session=str(parent.session_id))

    @staticmethod
    def count(value):
        return value if type(value) is int and 0 <= value <= 10**12 else None

    @staticmethod
    def duration(value):
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) and 0 <= parsed <= 3600 else None
        except (TypeError, ValueError, OverflowError):
            return None

    def usage(self, runs):
        if runs:
            try:
                self.client().request("POST", "/research/usage", {"runs": runs}, timeout=1)
            except PluginError:
                # Coverage exposes absent telemetry; forecasting remains independent.
                pass

    def parent_usage(self):
        parent = self.parent()
        try:
            calls = parent.get_activity_summary().get("api_call_count")
        except (AttributeError, TypeError):
            calls = None
        self.usage(
            [
                {
                    "session_id": str(parent.session_id),
                    "role": "parent",
                    "status": "running",
                    "api_calls": self.count(calls),
                    "input_tokens": self.count(getattr(parent, "session_prompt_tokens", None)),
                    "output_tokens": self.count(getattr(parent, "session_completion_tokens", None)),
                }
            ]
        )

    def batch(self, parent, roles, context, on_report=None):
        from tools.delegate_tool import delegate_task

        tasks, markers = [], []
        for role, body in zip(roles, context, strict=True):
            marker = "trading-role:" + role + ":" + str(uuid4())
            prompt = (Path(self.input_dir()) / f"prompt-{role}.txt").read_text(encoding="utf-8")
            with self.sessions_lock:
                self.pending[marker] = role
            markers.append(marker)
            tasks.append({"goal": marker + "\n" + prompt, "context": encode(body), "role": "leaf"})
        # Native output_schema would start another child turn per invalid report in this pin.
        # Strict parsing + the domain API validate once, without multiplying native children.
        # Schema/knowledge reads and deterministic geometry can require more than
        # three turns. The runtime's unchanged wall-clock deadline still bounds
        # the entire model run; truncated native reports remain unacceptable.
        raw = delegate_task(tasks=tasks, background=False, max_iterations=6, parent_agent=parent)
        try:
            entries = decode(raw)["results"]
            if len(entries) != len(roles) or {r["task_index"] for r in entries} != set(
                range(len(roles))
            ):
                raise ValueError("batch shape")
            by_index = {r["task_index"]: r for r in entries}
            reports, invalid, metrics = [], False, []
            for index, marker in enumerate(markers):
                metric = None
                try:
                    entry = by_index[index]
                    child_id = self.sessions.get(marker)
                    if child_id:
                        duration = self.duration(entry.get("duration_seconds"))
                        tokens = entry.get("tokens") or {}
                        tokens = tokens if isinstance(tokens, dict) else {}
                        status = entry.get("status", "unknown")
                        status = (
                            status
                            if status in {"completed", "failed", "error", "interrupted"}
                            else "unknown"
                        )
                        metric = {
                            "session_id": child_id,
                            "role": roles[index],
                            "status": "truncated" if entry.get("truncated") else status,
                            "duration_seconds": duration,
                            "api_calls": self.count(entry.get("api_calls")),
                            "input_tokens": self.count(tokens.get("input")),
                            "output_tokens": self.count(tokens.get("output")),
                        }
                    if entry["status"] != "completed" or entry.get("truncated") or not child_id:
                        raise ValueError("child incomplete or session missing")
                    report = decode(entry["summary"])
                    if not isinstance(report, dict):
                        raise ValueError("report shape")
                    child = {"child_id": child_id, "report": report}
                    if on_report:
                        on_report(child)
                    reports.append(child)
                except (KeyError, ValueError, TypeError, PluginError):
                    invalid = True
                    if metric and metric["status"] == "completed":
                        metric["status"] = "invalid"
                if metric:
                    metrics.append(metric)
            self.usage(metrics)
            if invalid:
                raise ValueError("at least one child failed")
            return reports
        except (KeyError, ValueError, TypeError):
            raise PluginError(
                "CHILD_REPORT_INVALID", "原生子任务未返回完整、可追溯的严格JSON报告"
            ) from None
        finally:
            with self.sessions_lock:
                for marker in markers:
                    self.pending.pop(marker, None)

    def analyze(self):
        with self.lock:
            parent = self.parent()
            client = self.client()
            context = self.context()
            if context.get("model_contract") == 4:
                if self.parent_run:
                    return context
                if self.failed:
                    raise PluginError(
                        "CHILD_BATCH_ALREADY_FAILED", "当前父会话已失败，等待程序有界恢复"
                    )
                from ._round import analyze_round

                try:
                    return analyze_round(self)
                except Exception:
                    self.failed = True
                    raise
            if context.get("schema_version") != 3:
                raise PluginError("LEGACY_FLOW_RETIRED", "旧预测委派流程已归档")
            if context.get("role") != "TIMEFRAME":
                raise PluginError("ROLE_FORBIDDEN", "综合任务复用现有周期预测，不重新委派全部周期")
            if self.structural_result:
                return self.structural_result
            if self.failed:
                raise PluginError("CHILD_BATCH_ALREADY_FAILED", "本attempt周期子任务已失败")
            role = context.get("timeframe")
            if role not in {"1h", "4h", "6h", "12h"}:
                raise PluginError("CONTEXT_INVALID", "周期范围无效")
            try:
                child = self.batch(parent, [role], [context])[0]
                self.structural_result = client.request(
                    "POST", "/research/prediction", child["report"]
                )
                return self.structural_result
            except Exception:
                self.failed = True
                raise
