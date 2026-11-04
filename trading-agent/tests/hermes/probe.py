"""Separate process loads the frozen Hermes tree. Provider/model transport is fixture-only."""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, sys.argv[1])
from agent.subagent_lifecycle import bind_subagent_parent  # noqa: E402
from hermes_cli.plugins import get_plugin_manager, invoke_hook  # noqa: E402
from tools.registry import registry  # noqa: E402

mode = sys.argv[2]
manager = get_plugin_manager()
manager.discover_and_load()
loaded = {row["name"]: row for row in manager.list_plugins()}
expected = {"trading-bridge"} if mode == "bridge" else {"trading-research", "trading-cube"}
for name in expected:
    assert loaded[name]["enabled"], loaded[name]
    assert not loaded[name]["error"], loaded[name]
if mode == "bridge":
    assert registry.get_entry("trading_latest") is not None
    assert registry.get_entry("trading_job_create") is not None
    assert registry.get_entry("research_context") is None
    response = json.loads(registry.get_entry("trading_latest").handler({}))
    assert response["error"]["code"] == "API_CREDENTIAL_MISSING"
    import agent.secret_scope as secret_scope

    secret_scope.get_secret = lambda name: "fixture-controller"

    class BridgeAPI(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer fixture-controller"
            data = json.dumps({"path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            assert self.path == "/v2/jobs"
            assert self.headers["Authorization"] == "Bearer fixture-controller"
            assert self.headers["Idempotency-Key"] == "fixture-controller-request"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body == {
                "kind": "explain",
                "prediction_id": "00000000-0000-0000-0000-000000000001",
            }
            data = json.dumps({"job_id": "fixture-job"}).encode()
            self.send_response(202)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plugin = next(
        (
            m
            for n, m in sys.modules.items()
            if getattr(m, "__file__", "") and str(m.__file__).endswith("trading-bridge/__init__.py")
        )
    )
    manifest = yaml.safe_load(Path(plugin.__file__).with_name("plugin.yaml").read_text())
    assert "trading_job_create" in manifest["provides_tools"]
    try:
        for version in ("v1", "v2/"):
            entries = {}
            ctx = SimpleNamespace(
                get_config=lambda *a, v=version: f"http://127.0.0.1:{server.server_port}/{v}",
                register_tool=lambda e=entries, **kw: e.update({kw["name"]: kw["handler"]}),
            )
            plugin.register(ctx)
            assert set(entries) == set(manifest["provides_tools"])
            assert all(registry.get_entry(name) is not None for name in entries)
            latest = json.loads(entries["trading_prediction_latest"]({"symbol": "BTCUSDT"}))
            assert (
                latest["path"] == "/v2/predictions/latest?symbol=BTCUSDT&timeframe=1h&parent=true"
            )
            legacy = json.loads(entries["trading_latest"]({}))
            assert legacy["path"] == "/v1/forecasts/latest"
            ident = "00000000-0000-0000-0000-000000000001"
            for tool, args, path in [
                ("trading_status", {}, "/v1/status"),
                ("trading_positions", {}, "/v2/positions"),
                ("trading_prediction_get", {"prediction_id": ident}, f"/v2/predictions/{ident}"),
                ("trading_intent_get", {"intent_id": ident}, f"/v2/intents/{ident}"),
                ("trading_forecast_get", {"forecast_id": ident}, f"/v1/forecasts/{ident}"),
                ("trading_job_get", {"job_id": ident}, f"/v1/jobs/{ident}"),
                ("trading_strategy_list", {}, "/v1/strategies"),
                (
                    "trading_experiment_get",
                    {"experiment_id": ident},
                    f"/v1/experiments/{ident}",
                ),
                ("trading_review_get", {"review_id": ident}, f"/v1/reviews/{ident}"),
            ]:
                assert json.loads(entries[tool](args))["path"] == path
            created = json.loads(
                entries["trading_job_create"](
                    {
                        "kind": "explain",
                        "prediction_id": ident,
                        "idempotency_key": "fixture-controller-request",
                    }
                )
            )
            assert created == {"job_id": "fixture-job"}
            invalid = json.loads(entries["trading_job_create"]({"kind": "review", "order": "BUY"}))
            assert invalid["error"]["code"] == "JOB_FIELDS_INVALID"
    finally:
        server.shutdown()
        server.server_close()
    print("bridge: controller requests, versioned queries, manifest and credential boundary")
    sys.exit(0)

if mode == "dialogue":
    from hermes_cli.config import load_config_readonly
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    config = load_config_readonly()
    chat_toolsets = _get_platform_tools(config, "telegram")
    assert "trading_research" not in chat_toolsets, chat_toolsets
    assert "terminal" not in chat_toolsets, chat_toolsets
    assert not chat_toolsets, chat_toolsets
    assert get_tool_definitions(enabled_toolsets=list(chat_toolsets), quiet_mode=True) == []
    cli_toolsets = _get_platform_tools(config, "cli")
    assert {"trading_research", "terminal"} <= cli_toolsets, cli_toolsets
    cli_definitions = get_tool_definitions(enabled_toolsets=list(cli_toolsets), quiet_mode=True)
    cli_names = {entry["function"]["name"] for entry in cli_definitions}
    assert {"research_context", "knowledge_search", "decision_submit"} <= cli_names
    assert not {"tool_search", "tool_describe", "tool_call"} & cli_names
    print("dialogue: Telegram has no tools; CLI research and sandbox toolsets remain available")
    sys.exit(0)
from agent.terminal_env_registry import get_provider  # noqa: E402
from tools import delegate_tool as native  # noqa: E402

provider = get_provider("cubesandbox")
assert provider and (not provider.is_available())
try:
    provider.create_environment(cwd="/tmp", timeout=1)
except RuntimeError:
    pass
else:
    raise AssertionError("Cube backend fell back despite absent attempt resource")
assert registry.get_entry("trading_latest") is None
if mode == "cube_cancel":
    kill = provider.create_environment.__globals__["kill_owned_sandbox"]
    metadata = {"attempt_id": "fixture", "fence": "1"}
    deleted = []
    mode_state = {"conflict": False, "slow": False}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            assert self.headers["X-API-Key"] == "fixture-key"
            if mode_state["slow"]:
                time.sleep(0.3)
            data = json.dumps({"metadata": {} if mode_state["conflict"] else metadata}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_DELETE(self):
            deleted.append(self.path)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    record = {
        "sandbox": {"sandboxID": "fixture-sandbox", "metadata": metadata},
        "config": {
            "api_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "fixture-key",
            "request_timeout": 0.05,
        },
    }
    try:
        kill(record)
        assert deleted == ["/sandboxes/fixture-sandbox"]
        mode_state["conflict"] = True
        try:
            kill(record)
        except PermissionError:
            pass
        else:
            raise AssertionError("foreign metadata was deleted")
        assert len(deleted) == 1
        mode_state["conflict"], mode_state["slow"] = (False, True)
        started = time.monotonic()
        try:
            kill(record)
        except Exception:
            assert time.monotonic() - started < 0.25
        else:
            raise AssertionError("unbounded management request")
        assert len(deleted) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(
        "cube cancellation: real loopback HTTP, ownership guard and bounded timeout; no live guest"
    )
    sys.exit(0)
metrics = []
stored, batches = ([], [])
assert mode == "structural"
assert registry.get_entry("forecast_submit") is None
context = {
    "schema_version": 3,
    "role": "TIMEFRAME",
    "symbol": "ETHUSDT",
    "timeframe": "4h",
    "structures": [],
    "horizon_rules": [],
    "children": [],
}


class API(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def respond(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer fixture-attempt-token"
        if self.path.startswith("/v2/research/knowledge/content?"):
            self.respond({"content": "fixture procedure", "next_offset": None})
            return
        if self.path == "/v2/research/context?prediction_id=fixture-case":
            self.respond(
                {"case": {"prediction_id": "fixture-case", "original_input": {"fixture": True}}}
            )
            return
        assert self.path == "/v2/research/context"
        self.respond(context)
        return

    def do_POST(self):
        assert self.headers["Authorization"] == "Bearer fixture-attempt-token"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v2/research/decision" and body.get("fixture_error") == "fields":
            self.respond(
                {
                    "error": {
                        "code": "REQUEST_INVALID",
                        "message": "private raw value",
                        "issues": [
                            {"path": ["prediction", "p_up"], "type": "decimal_parsing"},
                            {"path": ["PRIVATE_FIELD"], "type": "missing"},
                            {"path": ["prediction"], "type": "PRIVATE_VALUE"},
                        ],
                    }
                },
                422,
            )
            return
        if self.path == "/v2/research/decision" and body.get("fixture_error"):
            self.respond(
                {"error": {"code": "EVIDENCE_REFERENCE", "message": "private detail"}}, 422
            )
            return
        if self.path == "/v2/research/usage":
            metrics.extend(body["runs"])
            self.respond({"recorded": True})
        elif self.path == "/v2/research/knowledge/search":
            self.respond({"items": [{"id": "fixture-version", "status": "UNVERIFIED"}]})
        else:
            assert self.path in {"/v2/research/prediction", "/v2/research/decision"}
            stored.append(body)
            self.respond({"prediction_id": "fixture-structural-id"})
        return


server = ThreadingHTTPServer(("127.0.0.1", 0), API)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
os.environ["TA_API_URL"] = f"http://127.0.0.1:{server.server_port}/v2"
os.environ["TA_RESEARCH_TOKEN"] = "fixture-attempt-token"
os.environ["TA_INPUT_DIR"] = str(Path(os.environ["HERMES_HOME"]) / "inputs")


def children(tasks, schemas, credentials, **kwargs):
    assert kwargs["max_iterations"] == 6
    assert all(schema is None for schema in schemas)
    for index, task in enumerate(tasks):
        invoke_hook(
            "subagent_start",
            child_goal=task["goal"],
            child_session_id=f"child-{len(batches)}-{index}",
        )
    return ([], None)


def run_batch(batch, background):
    assert background is False
    roles = [task["goal"].split(":")[1] for task in batch.task_list]
    batches.append(roles)
    entries = []
    for index, role in enumerate(roles):
        report = {"schema_version": 3, "timeframe": role, "symbol": "ETHUSDT"}
        entries.append(
            {
                "task_index": index,
                "status": "completed",
                "truncated": False,
                "summary": json.dumps(report),
                "api_calls": 2,
                "duration_seconds": {"4h": 3, "6h": 5, "12h": 8, "1h": 2}[role],
                "tokens": {"input": 100, "output": 20},
            }
        )
    return json.dumps({"results": entries})


native._resolve_delegation_credentials = lambda *args: {"model": "fixture", "provider": "fixture"}
native._build_children = children
native._run_batch = run_batch
parent = SimpleNamespace(
    session_id="parent-fixture",
    _delegate_depth=0,
    get_activity_summary=lambda: {"api_call_count": 3},
    session_prompt_tokens=1000,
    session_completion_tokens=50,
)
try:
    with bind_subagent_parent(parent):
        case = json.loads(
            registry.get_entry("research_context").handler({"prediction_id": "fixture-case"})
        )
        assert case["case"]["original_input"] == {"fixture": True}
        invalid_context = json.loads(
            registry.get_entry("research_context").handler({"symbol": "OTHER"})
        )
        assert invalid_context["error"]["code"] == "CONTEXT_SCOPE_FORBIDDEN"
        response = json.loads(registry.get_entry("timeframes_analyze").handler({}))
        assert response == {"prediction_id": "fixture-structural-id"}, response
        assert json.loads(registry.get_entry("timeframes_analyze").handler({})) == response
        assert batches == [["4h"]] and len(stored) == 1
        assert metrics[0]["role"] == "4h"
        with bind_subagent_parent(SimpleNamespace(session_id="child-fixture", _delegate_depth=1)):
            for name in ("knowledge_search", "knowledge_expand", "tool_search", "tool_describe"):
                assert not any(
                    h and h.get("action") == "block"
                    for h in invoke_hook("pre_tool_call", tool_name=name)
                )
            assert json.loads(registry.get_entry("knowledge_search").handler({"query": "W底"}))[
                "items"
            ]
            assert json.loads(
                registry.get_entry("knowledge_expand").handler({"version_id": "fixture-version"})
            )["content"]
            assert any(
                h and h.get("action") == "block"
                for h in invoke_hook("pre_tool_call", tool_name="decision_submit")
            )
            from agent.tool_executor import _unwrap_tool_search_call

            scoped = SimpleNamespace(enabled_toolsets=["trading_research", "terminal"])
            for forbidden in ("decision_submit", "review_submit", "timeframes_analyze"):
                payload = {} if forbidden == "timeframes_analyze" else {"payload": {}}
                tool, arguments, blocked = _unwrap_tool_search_call(
                    scoped,
                    "tool_call",
                    {"calls": [{"name": forbidden, "arguments": payload}]},
                )
                assert blocked is None, blocked
                assert tool == forbidden
                assert any(
                    h and h.get("action") == "block"
                    for h in invoke_hook("pre_tool_call", tool_name=tool, tool_input=arguments)
                ), forbidden
            unknown, _, scope_block = _unwrap_tool_search_call(
                scoped,
                "tool_call",
                {"calls": [{"name": "browser", "arguments": {}}]},
            )
            assert scope_block or any(
                h and h.get("action") == "block"
                for h in invoke_hook("pre_tool_call", tool_name=unknown)
            )
        context["role"] = "PARENT"
        assert (
            json.loads(registry.get_entry("timeframes_analyze").handler({}))["error"]["code"]
            == "ROLE_FORBIDDEN"
        )
        assert json.loads(
            registry.get_entry("decision_submit").handler({"payload": {"fixture": True}})
        )["prediction_id"]
        assert len(stored) == 2 and batches == [["4h"]]
        frozen_file = Path(os.environ["TA_INPUT_DIR"]) / "context.json"
        fixture_input = "00000000-0000-0000-0000-000000000042"
        frozen_file.write_text(json.dumps({"input_id": fixture_input, "private": "never echo"}))
        rejected = json.loads(
            registry.get_entry("decision_submit").handler({"payload": {"fixture_error": True}})
        )["error"]
        assert rejected["code"] == "EVIDENCE_REFERENCE"
        assert "child_prediction_ids" in rejected["message"]
        assert fixture_input in rejected["message"]
        assert "private detail" not in rejected["message"]
        assert "never echo" not in rejected["message"]
        fields = json.loads(
            registry.get_entry("decision_submit").handler({"payload": {"fixture_error": "fields"}})
        )["error"]
        assert fields["code"] == "REQUEST_INVALID"
        assert "prediction.p_up:decimal_parsing" in fields["message"]
        assert "PRIVATE_" not in fields["message"] and "private raw" not in fields["message"]
        for invalid in ({"input_id": "private-invalid-id"}, None):
            if invalid is None:
                frozen_file.unlink()
            else:
                frozen_file.write_text(json.dumps(invalid))
            fallback = json.loads(
                registry.get_entry("decision_submit").handler({"payload": {"fixture_error": True}})
            )["error"]
            assert fallback["code"] == "EVIDENCE_REFERENCE"
            assert "private-invalid-id" not in fallback["message"]
        print(
            "structural: independent native child; frozen knowledge readable by child; "
            "parent decision only"
        )
        sys.exit(0)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
