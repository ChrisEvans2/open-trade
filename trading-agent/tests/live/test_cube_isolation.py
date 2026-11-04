"""Explicit real Cube + temporary PostgreSQL acceptance; no model or trading runtime."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from trading_agent.config import load_config
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.jobs.service import JobService
from trading_agent.runtime.deployment import REQUIRED_ISOLATION_CHECKS
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.runtime.sandboxes import CubeResources, sandbox_policy
from trading_agent.storage import schema as s

CONFIG = os.environ.get("TA_CUBE_LIVE_CONFIG")
REPORT = os.environ.get("TA_CUBE_LIVE_REPORT")
ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.live,
    pytest.mark.postgres,
    pytest.mark.skipif(not CONFIG or not REPORT, reason="Explicit live config/report required"),
]


async def test_real_cube_full_isolation(database, tmp_path):
    config = load_config(Path(CONFIG))
    key = os.environ["CUBE_API_KEY"]
    reference = Path(os.environ["TA_HERMES_REFERENCE"])
    report_path = Path(REPORT)
    assert not await asyncio.to_thread(report_path.exists), "Never overwrite acceptance evidence"
    checks = dict.fromkeys(sorted(REQUIRED_ISOLATION_CHECKS), False)
    evidence = {}
    owner = RuntimeOwnership(database)
    await owner.acquire()
    resources = CubeResources(config, key)
    jobs = JobService(database, owner)
    resources.jobs = jobs
    attempts = []
    sandbox_clients = []
    renewers = []
    foreign = None

    def record(name, passed, detail):
        checks[name] = bool(passed)
        evidence[name] = detail
        assert passed, name

    async def heartbeat(attempt):
        while True:
            await asyncio.sleep(10)
            if not await jobs.renew(attempt):
                return

    async def prepare(kind):
        async with database.transaction() as connection:
            await jobs.create(
                connection,
                kind,
                "live-isolation:" + uuid4().hex,
                {"acceptance_probe": True},
                await database.now(connection),
            )
        attempt = await jobs.claim((kind,))
        attempts.append(attempt)
        renewers.append(asyncio.create_task(heartbeat(attempt)))
        root = tmp_path / str(attempt.id)
        (root / "input").mkdir(parents=True)
        (root / "input/context.json").write_text('{"acceptance_probe":true}')
        (root / "input/task.txt").write_text("Isolation acceptance; no prediction or order.")
        await resources.prepare(attempt, root)
        from cubesandbox import Config, Sandbox

        sandbox = Sandbox(resources.records[attempt.id], Config(**resources.sdk_settings))
        sandbox_clients.append(sandbox)
        return attempt, root, sandbox

    async def command(sandbox, name, shell, *, wall_seconds=15):
        result = await asyncio.to_thread(
            sandbox.commands.run,
            shell,
            user=config.sandbox.execution_user,
            cwd="/workspace/scratch",
            timeout=wall_seconds,
        )
        # Commands are fixed nonsecret probes. No raw connection/profile/env is saved.
        detail = {
            "command": shell,
            "exit_code": result.exit_code,
            "stdout": result.stdout[:4096],
            "stderr": result.stderr[:4096],
        }
        record(name, result.exit_code == 0, detail)

    try:
        statuses = []
        async with httpx.AsyncClient(base_url=config.sandbox.base_url, trust_env=False) as client:
            for headers in ({}, {"X-API-Key": "invalid-live-probe"}, {"X-API-Key": key}):
                statuses.append((await client.get("/sandboxes", headers=headers)).status_code)
        record("server_auth_enforced", statuses == [401, 401, 200], {"statuses": statuses})
        from urllib.parse import urlsplit

        protected = urlsplit(config.sandbox.base_url).hostname == "127.0.0.1"
        protected &= config.sandbox.proxy_node_ip == "127.0.0.1"
        for binary in ("iptables", "ip6tables"):
            checked = await asyncio.to_thread(
                subprocess.run,
                [binary, "-C", "INPUT", "-j", "HERMES-CUBE-IN"],
                capture_output=True,
                timeout=5,
            )
            protected &= checked.returncode == 0
        record(
            "protected_transport",
            protected,
            {"loopback_management_and_data": True, "ipv4_ipv6_boundary_present": protected},
        )
        template = await resources.request("GET", "/templates/" + config.sandbox.template)
        request = template["createRequest"]
        raw_template = json.dumps(request, sort_keys=True)
        network = request.get("cube_network_config") or request.get("cubeNetworkConfig") or {}
        inherited_allow = any(network.get(k) for k in ("allow_out", "allowOut", "rules"))
        record(
            "template_no_inherited_allow",
            not inherited_allow,
            {
                "create_request_hash": hashlib.sha256(raw_template.encode()).hexdigest(),
                "network": network,
            },
        )
        attempt, root, sandbox = await prepare("EXPLAIN")
        ident = resources.records[attempt.id]["sandboxID"]
        info = await resources.request("GET", "/sandboxes/" + ident)
        await command(sandbox, "ordinary_user", "test $(id -u) -ne 0 && id")
        await command(
            sandbox,
            "input_readonly",
            "test ! -w /workspace && test ! -w /workspace/input && "
            "test ! -w /workspace/input/context.json && "
            "! chmod 666 /workspace/input/context.json 2>/dev/null",
        )
        await command(sandbox, "scratch_writable", "printf probe > probe.txt && test -w probe.txt")
        await command(sandbox, "sudo_denied", "! sudo -n true 2>/dev/null")
        await command(
            sandbox,
            "ipv6_disabled",
            "test $(cat /proc/sys/net/ipv6/conf/all/disable_ipv6) = 1 && "
            "test $(cat /proc/sys/net/ipv6/conf/default/disable_ipv6) = 1",
        )
        await command(
            sandbox,
            "no_host_mounts",
            "test ! -e /opt/open-trade && test ! -e /var/run/docker.sock && "
            "test ! -e /data/cubelet && cat /proc/mounts",
        )
        await command(
            sandbox,
            "no_credentials",
            "python3 - <<'GUEST'\n"
            "import os,pathlib\n"
            "for k in os.environ:\n"
            " words=['TOKEN','SECRET','PASSWORD','API_KEY','DATABASE_URL']\n"
            " assert not any(s in k for s in words), k\n"
            "assert not pathlib.Path('/workspace/input/auth.json').exists()\n"
            "print('guest environment contains no credential keys')\nGUEST",
        )
        await command(
            sandbox,
            "guest_management_denied",
            "python3 - <<'GUEST'\n"
            "import urllib.request,urllib.error,json,base64,struct,socket\n"
            "def denied(path,body,headers):\n"
            " try:\n"
            "  urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:49983'+path,data=body,headers=headers),timeout=2)\n"
            " except urllib.error.HTTPError as e:\n"
            "  assert e.code==401,(path,e.code); return\n"
            " raise AssertionError(path)\n"
            "body=json.dumps({'process':{'cmd':'/usr/bin/id','args':['-u']}}).encode()\n"
            "headers={'Content-Type':'application/connect+json',\n"
            " 'Authorization':'Basic '+base64.b64encode(b'root:').decode()}\n"
            "denied('/process.Process/Start',struct.pack('>BI',0,len(body))+body,headers)\n"
            "for body in [{},{'accessToken':'attacker-probe'}]:\n"
            " denied('/init',json.dumps(body).encode(),{'Content-Type':'application/json'})\n"
            "url='http://127.0.0.1:49983/files?path=/etc/shadow&username=root'\n"
            "try: urllib.request.urlopen(url,timeout=2)\n"
            "except urllib.error.HTTPError as e: assert e.code==401\n"
            "else: raise AssertionError('root file API reachable')\n"
            "s=socket.socket();s.settimeout(1)\n"
            "assert s.connect_ex(('127.0.0.1',8888))!=0;s.close()\n"
            "req=urllib.request.Request('http://127.0.0.1:49999/execute',data=b'{}',\n"
            " headers={'Content-Type':'application/json'})\n"
            "try: urllib.request.urlopen(req,timeout=2)\n"
            "except urllib.error.HTTPError as e: assert e.code in [404,405,501]\n"
            "else: raise AssertionError('code interpreter reachable')\n"
            "print('privileged guest APIs denied')\nGUEST",
        )
        await command(
            sandbox,
            "research_public_access",
            "python3 - <<'GUEST'\n"
            "import urllib.request\n"
            "with urllib.request.urlopen('https://example.com',timeout=5) as r:\n"
            " assert r.status==200\n"
            "print('public HTTPS succeeded')\nGUEST",
        )
        targets = ["10.100.0.1", "169.254.169.254", "127.0.0.1", "172.17.0.1"]
        targets += [value.split("/")[0] for value in config.sandbox.denied_networks]
        await command(
            sandbox,
            "sensitive_addresses_denied",
            "python3 - <<'GUEST'\n"
            "import socket\n"
            f"targets={targets!r}\n"
            "for host in targets:\n"
            " for port in [80,3000,8089,9999]:\n"
            "  s=socket.socket();s.settimeout(.15)\n"
            "  assert s.connect_ex((host,port))!=0,(host,port);s.close()\n"
            "print('private, metadata, host management and sensitive public IP denied')\nGUEST",
        )
        await command(
            sandbox,
            "resource_limits",
            "python3 - <<'GUEST'\n"
            "import os\n"
            f"assert 0<os.cpu_count()<={config.sandbox.cpu_count}\n"
            "mem=int(open('/proc/meminfo').readline().split()[1])\n"
            f"assert 0<mem<={config.sandbox.memory_mib}*1024\n"
            "s=os.statvfs('/workspace/scratch')\n"
            f"assert 0<s.f_blocks*s.f_frsize<={config.sandbox.disk_mib}*1048576\n"
            "print('CPU, guest memory and writable filesystem within configured caps')\nGUEST",
        )
        assert 0 < info["cpuCount"] <= config.sandbox.cpu_count
        assert 0 < info["memoryMB"] <= config.sandbox.memory_mib
        # Actual Hermes providers: two child clients, normal cleanup, symlink bounds,
        # absent attempt context, and changed fence. No model is called.
        home = tmp_path / "probe-home"
        shutil.copytree(ROOT / "integrations/hermes/trading-cube", home / "plugins/trading-cube")
        (home / "config.yaml").write_text("plugins:\n  enabled: [trading-cube]\n")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "HERMES_HOME": str(home),
            "PYTHONNOUSERSITE": "1",
            "HERMES_ENABLE_PROJECT_PLUGINS": "0",
            "TA_CUBE_SANDBOX_FILE": str(root / ".cube.secrets.json"),
            "TA_ATTEMPT_ID": str(attempt.id),
            "TA_JOB_ID": str(attempt.job_id),
            "TA_FENCE": str(attempt.fence),
        }
        probe = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                str(ROOT / "tests/hermes/cube_isolation_probe.py"),
                str(reference),
                str(tmp_path / "fetched"),
            ],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.returncode == 0, "Native Cube probe failed; see private acceptance log"
        native = json.loads(probe.stdout)
        for name in ("file_bridge_bounds", "shared_child_lifetime", "no_local_fallback"):
            record(name, native.get(name) is True, {"native_provider_probe": True})
        async with database.transaction() as connection:
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(fence=attempt.fence + 1)
            )
        with pytest.raises(DomainError):
            await resources.renew(attempt, 30)
        record(
            "stale_attempt_denied",
            native.get("stale_attempt_denied") is True,
            {"native_changed_fence_denied": True, "real_postgres_renewal_denied": True},
        )
        assert await resources.cleanup(attempt)
        formal, _, formal_sandbox = await prepare("PREDICT_TIMEFRAME")
        await command(
            formal_sandbox,
            "formal_egress_denied",
            "python3 - <<'GUEST'\n"
            "import socket,urllib.request\n"
            "s=socket.socket();s.settimeout(1);assert s.connect_ex(('1.1.1.1',443))!=0;s.close()\n"
            "try: urllib.request.urlopen('https://1.1.1.1',timeout=2)\n"
            "except Exception: pass\n"
            "else: raise AssertionError('formal public egress allowed')\n"
            "query=bytes.fromhex('123401000001000000000000')\n"
            "query+=bytes.fromhex('076578616d706c6503636f6d0000010001')\n"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(1)\n"
            "s.sendto(query,('1.1.1.1',53))\n"
            "try: s.recvfrom(512)\n"
            "except (TimeoutError,ConnectionError): pass\n"
            "else: raise AssertionError('formal DNS egress allowed')\n"
            "finally: s.close()\n"
            "print('formal HTTP, direct public TCP and UDP DNS denied')\nGUEST",
        )
        # Preserve an explicitly foreign sandbox while a new DB owner recovers this attempt.
        foreign_meta = {"project": "hermes-isolation-foreign-probe", "attempt_id": str(uuid4())}
        foreign = await resources.request(
            "POST",
            "/sandboxes",
            {
                "templateID": config.sandbox.template,
                "timeout": 120,
                "metadata": foreign_meta,
                "envVars": {},
                "volumeMounts": [],
                "lifecycle": {"onTimeout": "kill", "autoResume": False},
                **sandbox_policy(config.sandbox, "PREDICT_TIMEFRAME"),
            },
        )
        foreign["metadata"] = foreign_meta
        await owner.release()
        owner = RuntimeOwnership(database)
        await owner.acquire()
        resources.jobs = JobService(database, owner)
        await resources.recover()
        formal_id = resources.records[formal.id]["sandboxID"]
        gone = await resources.request("GET", "/sandboxes/" + formal_id, missing=True) is None
        kept = (
            await resources.request("GET", "/sandboxes/" + foreign["sandboxID"], missing=True)
            is not None
        )
        record(
            "owned_crash_recovery",
            gone and kept,
            {
                "new_real_pg_owner": True,
                "owned_sandbox_deleted": gone,
                "foreign_sandbox_preserved": kept,
            },
        )
    finally:
        for task in renewers:
            task.cancel()
        await asyncio.gather(*renewers, return_exceptions=True)
        for client in sandbox_clients:
            client.close()
        cleanup_ok = True
        for attempt in attempts:
            try:
                cleanup_ok &= await resources.cleanup(attempt)
            except Exception:
                cleanup_ok = False
        if foreign:
            current = await resources.request(
                "GET", "/sandboxes/" + foreign["sandboxID"], missing=True
            )
            if (
                current is not None
                and current.get("metadata", {}).get("project") == foreign["metadata"]["project"]
            ):
                await resources.request("DELETE", "/sandboxes/" + foreign["sandboxID"])
        await resources.close()
        await owner.release()
        report = {
            "kind": "live",
            "created_at": datetime.now(UTC).isoformat(),
            "template": config.sandbox.template,
            "sdk_version": config.sandbox.sdk_version,
            "isolation_config_hash": content_hash(
                config.sandbox.model_dump(
                    mode="json", exclude={"isolation_verified", "isolation_report"}
                )
            ),
            "checks": checks,
            "evidence": evidence,
            "cleanup_confirmed": cleanup_ok,
            "database_scope": "temporary isolated PostgreSQL acceptance database",
            "model_called": False,
            "trading_runtime_started": False,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            report_path.write_text, json.dumps(report, indent=2, ensure_ascii=False)
        )
        await asyncio.to_thread(report_path.chmod, 0o600)
    assert cleanup_ok and all(checks.values())
