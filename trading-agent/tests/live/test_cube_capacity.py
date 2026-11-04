"""Opt-in two-sandbox capacity probe; synthetic memory, no model or trading facts."""

import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from trading_agent.config import load_config
from trading_agent.runtime.sandboxes import (
    CubeResources,
    ownership_metadata,
    sandbox_policy,
    sdk_config,
)

CONFIG = os.environ.get("TA_CUBE_CAPACITY_CONFIG")
REPORT = os.environ.get("TA_CUBE_CAPACITY_REPORT")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not CONFIG or not REPORT, reason="Explicit capacity config/report required"),
]


def host_sample():
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    pressure = {}
    for line in Path("/proc/pressure/memory").read_text().splitlines():
        kind, *fields = line.split()
        pressure[kind] = dict(field.split("=") for field in fields)
    return {
        "available_mib": int(values["MemAvailable"].split()[0]) / 1024,
        "oom_kill": int(vm["oom_kill"]),
        "pressure": pressure,
    }


def component_states():
    # This opt-in probe targets the local v0.7.3 all-in-one deployment.
    units = (
        "cube-sandbox-cube-api.service",
        "cube-sandbox-cubelet.service",
        "cube-sandbox-cube-proxy.service",
        "cube-sandbox-cube-egress.service",
    )
    return {
        unit: subprocess.check_output(
            ["systemctl", "show", "--value", "-p", "ActiveState", unit], text=True, timeout=5
        ).strip()
        for unit in units
    }


def test_two_sandboxes_with_bounded_memory_load(tmp_path):
    from cubesandbox import Config, Sandbox

    config = load_config(Path(CONFIG))
    key = os.environ["CUBE_API_KEY"]
    report_path = Path(REPORT)
    assert not report_path.exists(), "Never overwrite acceptance evidence"
    sdk = sdk_config(config.sandbox, key)
    resources = SimpleNamespace(config=config, sdk_settings=sdk)
    report = {
        "kind": "synthetic_live_capacity",
        "created_at": datetime.now(UTC).isoformat(),
        "guest_memory_mib": 512,
        "synthetic_host_memory_mib_per_agent": 256,
        "hold_seconds_per_round": 45,
        "model_called": False,
        "business_runtime_started": False,
        "rounds": [],
        "before": host_sample(),
        "components_before": component_states(),
    }
    assert all(state == "active" for state in report["components_before"].values()), (
        "All Cube components must be active before capacity acceptance"
    )
    floor = 768
    run = uuid4().hex
    cleanup_confirmed = True
    with httpx.Client(
        base_url=config.sandbox.base_url,
        headers={"X-API-Key": key},
        timeout=20,
        trust_env=False,
    ) as client:
        assert client.get("/sandboxes").json() == [], "Exclusive idle capacity window required"
        try:
            for cycle in range(3):
                assert host_sample()["available_mib"] >= 2304, "Insufficient starting headroom"
                owned, sandboxes, agents, futures = [], [], [], []
                samples = []
                round_report = {
                    "round": cycle + 1,
                    "passed": False,
                    "both_commands_succeeded": False,
                    "samples": samples,
                }
                report["rounds"].append(round_report)
                marker = f"capacity-{run}-{cycle}"
                guest_code = (
                    "import os,time,pathlib,sys; "
                    "pathlib.Path('/workspace/scratch/capacity.pid').write_text(str(os.getpid())); "
                    "owner=pathlib.Path('/workspace/scratch/capacity.owner'); "
                    "owner.write_text(sys.argv[1]); "
                    "b=bytearray(512*1048576); "
                    "b[::4096]=b'x'*len(b[::4096]); "
                    "time.sleep(45); assert owner.read_text()==sys.argv[1]; "
                    "print('capacity-load-complete')"
                )
                host_code = (
                    "import time; b=bytearray(256*1048576); "
                    "b[::4096]=b'x'*len(b[::4096]); "
                    "print('ready',flush=True); time.sleep(90)"
                )
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
                try:
                    for number in range(2):
                        metadata = {
                            "project": "hermes-cube-capacity-acceptance",
                            "job_id": run,
                            "attempt_id": f"{cycle}-{number}",
                            "fence": "1",
                        }
                        response = client.post(
                            "/sandboxes",
                            json={
                                "templateID": config.sandbox.template,
                                "timeout": 180,
                                "metadata": metadata,
                                "envVars": {},
                                "volumeMounts": [],
                                "lifecycle": {"onTimeout": "kill", "autoResume": False},
                                **sandbox_policy(config.sandbox, "PREDICT_TIMEFRAME"),
                            },
                        )
                        response.raise_for_status()
                        raw = response.json()
                        ident = raw["sandboxID"]
                        owned.append((ident, metadata))
                        current = client.get("/sandboxes/" + ident)
                        current.raise_for_status()
                        assert ownership_metadata(current.json()) == metadata
                        folder = tmp_path / f"{cycle}-{number}"
                        (folder / "input").mkdir(parents=True)
                        (folder / "input/context.json").write_text('{"acceptance_probe":true}')
                        (folder / "input/task.txt").write_text(
                            "Synthetic capacity; no prediction/order."
                        )
                        CubeResources._populate(resources, raw, folder)
                        sandboxes.append(Sandbox(raw, Config(**sdk)))
                    assert owned[0][0] != owned[1][0]
                    for number, sandbox in enumerate(sandboxes):
                        agent = subprocess.Popen(
                            [sys.executable, "-c", host_code],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True,
                        )
                        agents.append(agent)
                        assert agent.stdout.readline().strip() == "ready"
                        futures.append(
                            pool.submit(
                                sandbox.commands.run,
                                "python3 -c "
                                + shlex.quote(guest_code)
                                + " "
                                + marker
                                + f"-{number}",
                                user=config.sandbox.execution_user,
                                cwd="/workspace/scratch",
                                timeout=75,
                            )
                        )
                    started = time.monotonic()
                    while not all(future.done() for future in futures):
                        sample = host_sample()
                        samples.append(sample)
                        assert sample["available_mib"] >= floor, "Capacity headroom below 768 MiB"
                        assert sample["oom_kill"] == report["before"]["oom_kill"]
                        assert all(agent.poll() is None for agent in agents)
                        assert time.monotonic() - started < 85, (
                            "Bounded guest command exceeded budget"
                        )
                        if len(samples) % 15 == 1:
                            for ident, metadata in owned:
                                current = client.get("/sandboxes/" + ident)
                                current.raise_for_status()
                                assert ownership_metadata(current.json()) == metadata
                                client.post(
                                    "/sandboxes/" + ident + "/timeout", json={"timeout": 180}
                                ).raise_for_status()
                        time.sleep(1)
                    for future in futures:
                        result = future.result()
                        assert result.exit_code == 0
                        assert "capacity-load-complete" in result.stdout
                    round_report["both_commands_succeeded"] = True
                    assert samples
                    assert max(float(s["pressure"]["full"]["avg10"]) for s in samples) < 1
                    round_report["passed"] = True
                    round_report["elapsed_seconds"] = round(time.monotonic() - started, 2)
                finally:
                    if samples:
                        round_report["minimum_available_mib"] = round(
                            min(s["available_mib"] for s in samples), 1
                        )
                        round_report["maximum_full_pressure_avg10"] = max(
                            float(s["pressure"]["full"]["avg10"]) for s in samples
                        )
                    for agent in agents:
                        if agent.poll() is None:
                            agent.terminate()
                            try:
                                agent.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                agent.kill()
                                agent.wait(timeout=5)
                        if agent.stdout:
                            agent.stdout.close()
                    # Destroy only this probe's resources, including on low-headroom abort.
                    for ident, metadata in owned:
                        current = client.get("/sandboxes/" + ident)
                        if current.status_code == 404:
                            continue
                        current.raise_for_status()
                        if ownership_metadata(current.json()) != metadata:
                            cleanup_confirmed = False
                            continue
                        client.delete("/sandboxes/" + ident).raise_for_status()
                        cleanup_confirmed &= client.get("/sandboxes/" + ident).status_code == 404
                    for sandbox in sandboxes:
                        sandbox.close()
                    pool.shutdown(wait=True)
        finally:
            report["after"] = host_sample()
            report["components_after"] = component_states()
            report["cleanup_confirmed"] = cleanup_confirmed
            report["passed"] = (
                len(report["rounds"]) == 3
                and all(r["passed"] for r in report["rounds"])
                and cleanup_confirmed
                and all(state == "active" for state in report["components_after"].values())
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x") as stream:
                json.dump(report, stream, indent=2)
            report_path.chmod(0o600)
    assert report["passed"]
