"""Native public lifecycle fan-out with per-role wall clocks and durable retries."""

import time
from pathlib import Path
from uuid import uuid4

from ._http import PluginError, decode, encode


def analyze_round(workflow):
    from agent.subagent_lifecycle import SubagentLaunchRequest

    client = workflow.client()
    lifecycle = workflow.lifecycle
    if lifecycle is None:
        raise PluginError("NATIVE_LIFECYCLE_MISSING", "固定Hermes公共子Agent生命周期不可用")
    while True:
        state = client.request("GET", "/research/analysis")
        if state["wait_seconds"] or state["awaiting_data"]:
            time.sleep(min(2, float(state["wait_seconds"] or 1)))
            continue
        roles = state["needed"]
        if not roles:
            try:
                parent = client.request(
                    "POST", "/research/analysis/start", {"role": "parent"}, timeout=30
                )
            except PluginError as exc:
                if exc.code in {"MISSING_TIMEFRAME", "MINUTE_DATA_PENDING", "ANALYSIS_WINDOW"}:
                    time.sleep(1)
                    continue
                raise
            workflow.parent_run = parent["run"]
            return parent["context"]
        if any(state["counts"].get(role, 0) >= state["limit"] for role in roles):
            raise PluginError("ROLE_RETRIES_EXHAUSTED", "所需周期重试次数已用尽")
        preparations = []
        pending = []
        failed = False
        try:
            # Freeze in order to keep short DB lock waits bounded; launch each
            # native child immediately so inference overlaps later preparation.
            for role in roles:
                try:
                    stage = client.request(
                        "POST", "/research/analysis/start", {"role": role}, timeout=30
                    )
                    if stage.get("reused"):
                        continue
                    stage["received_at"] = time.monotonic()
                    preparations.append(stage)
                except PluginError as exc:
                    if exc.code not in {
                        "MINUTE_DATA_PENDING",
                        "ANALYSIS_WINDOW",
                        "ROLE_PREPARATION_FAILED",
                        "API_HTTP_503",
                        "INPUT_SCOPE",
                        "REQUEST_TIMEOUT",
                    }:
                        raise
                    continue
                run = stage["run"]
                marker = "trading-role:" + role + ":" + str(uuid4())
                with workflow.sessions_lock:
                    workflow.pending[marker] = role
                    workflow.pending_runs[marker] = run
                prompt = (Path(workflow.input_dir()) / f"prompt-{role}.txt").read_text(
                    encoding="utf-8"
                )
                context = encode(stage["context"])
                if len(context) > 30000:
                    # Native launch has a 32k character cap. Fetch the complete
                    # authorized input via the bound tool instead of truncation.
                    context = encode(
                        {
                            "model_contract": 4,
                            "role": role,
                            "symbol": stage["context"].get("symbol"),
                            "read_research_context": True,
                        }
                    )
                    prompt = "先调用research_context读取本次角色的完整冻结输入。\n" + prompt
                handle = lifecycle.launch(
                    SubagentLaunchRequest(
                        goal=marker + "\n" + prompt,
                        context=context,
                        role="leaf",
                        correlation_id=marker,
                    )
                )
                pending.append(
                    {
                        "handle": handle,
                        "stage": stage,
                        "marker": marker,
                        "until": stage["received_at"] + float(stage["execution_seconds"]),
                    }
                )
            while pending:
                for entry in tuple(pending):
                    handle, stage = entry["handle"], entry["stage"]
                    native = lifecycle.result(handle)
                    if not native.ready and time.monotonic() < entry["until"]:
                        continue
                    code = None
                    if not native.ready:
                        lifecycle.cancel(handle, reason="120 second research execution deadline")
                        settled = lifecycle.wait(handle, timeout_seconds=3)
                        if not settled.completed:
                            client.request(
                                "POST",
                                "/research/analysis/fail",
                                {"run": stage["run"], "code": "CHILD_CANCELLATION_PENDING"},
                            )
                            # Never start replacement children while an old child
                            # can still issue tools. Runtime terminates this process.
                            raise PluginError("CHILD_CANCELLATION_PENDING", "子任务取消尚未确认")
                        native = lifecycle.result(handle)
                        code = "AGENT_TIMEOUT"
                    if native.terminal_state.value != "SUCCEEDED":
                        code = code or "CHILD_REPORT_INVALID"
                    try:
                        if code:
                            raise PluginError(code, "原生子任务未成功结束")
                        report = decode(native.summary or "")
                        if not isinstance(report, dict):
                            raise ValueError("not an object")
                        client.request("POST", "/research/analysis/complete", {"run": stage["run"]})
                        client.request(
                            "POST",
                            "/research/analysis/prediction",
                            report,
                            run=stage["run"],
                            timeout=15,
                        )
                    except (PluginError, ValueError, TypeError) as exc:
                        code = exc.code if isinstance(exc, PluginError) else "CHILD_REPORT_INVALID"
                        client.request(
                            "POST", "/research/analysis/fail", {"run": stage["run"], "code": code}
                        )
                        failed = True
                    child_id = workflow.sessions.get(entry["marker"])
                    if child_id:
                        workflow.usage(
                            [
                                {
                                    "session_id": child_id,
                                    "role": stage["role"],
                                    "status": "invalid" if code else "completed",
                                    "duration_seconds": workflow.duration(
                                        native.tool_execution_summary.get("duration_seconds")
                                    ),
                                    "api_calls": workflow.count(
                                        native.usage_metadata.get("api_calls")
                                    ),
                                }
                            ]
                        )
                    pending.remove(entry)
                if pending:
                    time.sleep(0.05)
        finally:
            for entry in pending:
                lifecycle.cancel(entry["handle"], reason="research batch stopped")
                try:
                    client.request(
                        "POST",
                        "/research/analysis/fail",
                        {"run": entry["stage"]["run"], "code": "CHILD_BATCH_INTERRUPTED"},
                    )
                except PluginError:
                    pass  # Host also supervises unacknowledged child deadlines.
        if failed or not preparations:
            state = client.request("GET", "/research/analysis")
            counts = [state["counts"].get(role, 0) for role in state["needed"]]
            if counts and max(counts) >= state["limit"]:
                raise PluginError("ROLE_RETRIES_EXHAUSTED", "所需周期重试次数已用尽")
            time.sleep(15 if counts and max(counts) > 1 else 5)
