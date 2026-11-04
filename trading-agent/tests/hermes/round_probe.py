"""Pinned native public lifecycle and loaded plugin, fixture API/child transport only."""

import json
import os
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
os.environ["TA_ANALYSIS_ROUND"] = "1"
from agent import secret_scope
from agent import subagent_lifecycle as lifecycle
from hermes_cli.plugins import get_plugin_manager, invoke_hook
from tools import delegate_tool
from tools.registry import registry

secret_scope.get_secret = lambda name: "fixture-token"
manager = get_plugin_manager()
manager.discover_and_load()
for entry in manager.list_plugins():
    if entry["name"] in {"trading-research", "trading-cube"}:
        assert not entry["error"], entry
roles = ("1h", "4h", "6h", "12h")
counts = Counter()
stored = set()
runs = {}
active = 0
peak = 0
completed = []
parent_starts = 0
failures = []
lock = threading.Lock()
barrier = threading.Barrier(4)


def candle_payload(role=None):
    if role == "parent":
        return {"input_kind": "CHILD_SYNTHESIS", "children": [{"ref": "child." + r} for r in roles]}
    return {
        "model_bars": [{"fixture": "closed", "n": i} for i in range(30)],
        "forming_candle": {"fixture": "current", "status": "PROVISIONAL", "confirm": "0"},
        "provisional_features": [{"name": "rsi", "status": "PROVISIONAL", "value": 62}],
    }


def assert_candles(value):
    assert len(value["model_bars"]) == 30
    assert value["forming_candle"] == candle_payload()["forming_candle"]
    assert value["provisional_features"] == candle_payload()["provisional_features"]


class API(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer fixture-token"
        if self.path.endswith("/context?child_ref=child.4h"):
            assert runs[self.headers["X-Analysis-Run"]] == "parent"
            self.respond({"source_child_ref": "child.4h", "context": candle_payload()})
            return
        if self.path.endswith("/context"):
            run = self.headers["X-Analysis-Run"]
            if run:
                self.respond(
                    {
                        "model_contract": 4,
                        "role": runs[run],
                        "full_evidence": "fixture",
                        **candle_payload(runs[run]),
                    }
                )
                return
        self.respond(
            dict(
                model_contract=4,
                role="COORDINATOR",
                needed=[r for r in roles if r not in stored],
                wait_seconds=0,
                awaiting_data=[],
                counts=dict(counts),
                limit=3,
            )
        )

    def do_POST(self):
        global parent_starts
        assert self.headers["Authorization"] == "Bearer fixture-token"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/start"):
            role = body["role"]
            if sys.argv[2] == "expanded" and role == "parent" and counts["6h"] == 1:
                stored.remove("6h")
                self.respond(
                    {
                        "error": {
                            "code": "MISSING_TIMEFRAME",
                            "message": "fixture changed dependency",
                        }
                    },
                    409,
                )
                return
            counts[role] += 1
            run = f"{role}-{counts[role]}"
            runs[run] = role
            if role == "parent":
                assert len(stored) == 4
                parent_starts += 1
            self.respond(
                dict(
                    role=role,
                    run=run,
                    context=dict(
                        model_contract=4,
                        role=role,
                        number=counts[role],
                        child_options=["child." + r for r in stored],
                        oversized_fixture="x" * 40000 if sys.argv[2] == "large" else "",
                        **candle_payload(role),
                    ),
                    execution_seconds=0.15
                    if sys.argv[2] in {"timeout", "pending"} and run == "4h-1"
                    else 10,
                )
            )
        elif self.path.endswith("/prediction"):
            run = self.headers["X-Analysis-Run"]
            role = runs[run]
            assert body == {"fixture_role": role}
            if sys.argv[2] == "retry" and run == "4h-1":
                self.respond({"error": {"code": "STRUCTURE_CHANGED", "message": "fixture"}}, 409)
            else:
                stored.add(role)
                self.respond({"published": True})
        elif self.path.endswith("/fail"):
            failures.append(body["code"])
            self.respond({"recorded": True})
        elif self.path.endswith("/decision"):
            assert self.headers["X-Analysis-Run"] == "parent-1"
            assert len(stored) == 4
            self.respond({"published": True})
        elif self.path.endswith("/search"):
            run = self.headers["X-Analysis-Run"]
            assert runs[run] in roles
            self.respond({"items": []})
        else:
            assert self.path.endswith(("/complete", "/fail", "/usage")), self.path
            self.respond({"recorded": True})


server = ThreadingHTTPServer(("127.0.0.1", 0), API)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
os.environ["TA_API_URL"] = f"http://127.0.0.1:{server.server_port}/v2"
os.environ["TA_INPUT_DIR"] = str(Path(os.environ["HERMES_HOME"]) / "inputs")


def build(**kwargs):
    role = kwargs["goal"].split(":")[1]
    assert len(kwargs["context"]) <= 32000
    if sys.argv[2] == "large":
        assert json.loads(kwargs["context"])["read_research_context"]
    else:
        assert_candles(json.loads(kwargs["context"]))
    child = SimpleNamespace(
        session_id=kwargs["goal"].splitlines()[0],
        _delegate_depth=1,
        _subagent_id=kwargs["goal"],
        provider="fixture",
        model="fixture",
        _delegate_role="leaf",
        cancelled=threading.Event(),
        number=counts[role],
        role=role,
    )
    invoke_hook("subagent_start", child_goal=kwargs["goal"], child_session_id=child.session_id)
    return child


def worker(index, goal, child, parent):
    global active, peak
    with lock:
        active += 1
        peak = max(peak, active)
    try:
        if child.number == 1:
            barrier.wait(timeout=5)
        with lifecycle.bind_subagent_parent(child):
            if sys.argv[2] == "large":
                complete = json.loads(registry.get_entry("research_context").handler({}))
                assert complete["role"] == child.role and complete["full_evidence"] == "fixture"
                assert_candles(complete)
            response = json.loads(
                registry.get_entry("knowledge_search").handler({"query": "fixture"})
            )
            assert "error" not in response, response
        if sys.argv[2] == "pending" and child.role == "4h":
            time.sleep(5)
            return {"status": "interrupted", "summary": "partial"}
        if sys.argv[2] == "timeout" and child.role == "4h" and child.number == 1:
            assert child.cancelled.wait(3)
            return {"status": "interrupted", "summary": "partial"}
        time.sleep({"1h": 0.04, "4h": 0.12, "6h": 0.08, "12h": 0.01}[child.role])
        completed.append(child.role)
        return {
            "status": "completed",
            "summary": json.dumps({"fixture_role": child.role}),
            "api_calls": 2,
            "duration_seconds": 0.1,
        }
    finally:
        with lock:
            active -= 1


delegate_tool._build_child_preserving_parent_tools = build
delegate_tool._run_child_lifecycle = worker
lifecycle.request_hard_interrupt = lambda child, *args, **kwargs: child.cancelled.set() or True
parent = SimpleNamespace(
    session_id="fixture-parent",
    _delegate_depth=0,
    get_activity_summary=lambda: {"api_call_count": 3},
)

try:
    with lifecycle.bind_subagent_parent(parent):
        response = json.loads(registry.get_entry("timeframes_analyze").handler({}))
        if sys.argv[2] == "pending":
            assert response["error"]["code"] == "CHILD_CANCELLATION_PENDING", response
            assert "CHILD_CANCELLATION_PENDING" in failures
            assert counts["4h"] == 1 and counts["parent"] == 0
            print("unconfirmed cancellation aborts the batch without replacement children")
            sys.exit(0)
        assert "error" not in response, response
        assert response["role"] == "parent"
        assert response["input_kind"] == "CHILD_SYNTHESIS"
        assert "model_bars" not in response and "forming_candle" not in response
        detail = json.loads(
            registry.get_entry("research_context").handler({"child_ref": "child.4h"})
        )
        assert detail["source_child_ref"] == "child.4h"
        assert_candles(detail["context"])
        assert json.loads(
            registry.get_entry("decision_submit").handler({"payload": {"fixture": True}})
        ) == {"published": True}
    assert peak == 4, peak
    changed = "6h" if sys.argv[2] == "expanded" else "4h"
    expected = dict.fromkeys(roles, 1) | {"parent": 1}
    if sys.argv[2] != "large":
        expected[changed] = 2
    assert counts == expected, counts
    assert parent_starts == 1 and len(stored) == 4 and active == 0
    assert completed[0] == "12h", completed
    print(
        "native lifecycle: four concurrent children, out-of-order publication; "
        "scoped reads, only 4h retry, one parent; fixture transport"
    )
finally:
    server.shutdown()
    server.server_close()
    thread.join(2)
