from __future__ import annotations

import asyncio
import importlib.metadata
import ipaddress
import math
import os
import secrets
import shlex
import stat
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import sqlalchemy as sa

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import canonical_json
from trading_agent.runtime.logging import attempt_event
from trading_agent.storage import schema as s

PROJECT = "personal-trading-agent-v1"
OWNERSHIP_FIELDS = ("project", "job_id", "attempt_id", "fence")
PRIVATE_NETWORKS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def ownership_metadata(raw):
    # Cube adds internal template/component labels to GET/list metadata.
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return {name: metadata.get(name) for name in OWNERSHIP_FIELDS}


def sandbox_policy(config, kind):
    research = kind in {"EXPLAIN", "REVIEW_DAILY"}
    denied = set(PRIVATE_NETWORKS)
    for value in config.denied_networks:
        network = ipaddress.ip_network(value, strict=False)
        if network.version != 4:
            raise DomainError(
                "SANDBOX_NETWORK_INVALID", "Cube的deny_out仅接受IPv4；IPv6由模板禁用", 503
            )
        denied.add(str(network))
    if not research:
        denied.add("0.0.0.0/0")
    return {
        "allow_internet_access": research,
        "network": {
            "allowOut": [],
            "denyOut": sorted(denied),
            "rules": [],
            "allowPublicTraffic": False,
        },
    }


def sdk_config(config, key):
    parsed = urlsplit(config.base_url or "")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise DomainError("CUBE_CONFIGURATION_INVALID", "Cube管理地址无效", 503)
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise DomainError(
            "CUBE_TRANSPORT_UNPROTECTED", "远程Cube管理通道必须使用HTTPS或本机隧道", 503
        )
    # SDK 0.7.0 data-plane URLs are HTTP. Only loopback tunnel endpoints are accepted.
    if config.proxy_node_ip != "127.0.0.1":
        raise DomainError("CUBE_TRANSPORT_UNPROTECTED", "Cube数据面必须经127.0.0.1保护隧道", 503)
    if config.sdk_version != "0.7.0" or not config.template or not key:
        raise DomainError("CUBE_CONFIGURATION_INCOMPLETE", "Cube版本、模板和认证尚未配置", 503)
    if importlib.metadata.version("cubesandbox") != config.sdk_version:
        raise DomainError("CUBE_SDK_MISMATCH", "Cube SDK版本与部署锁不一致", 503)
    return {
        "api_url": config.base_url,
        "api_key": key,
        "template_id": config.template,
        "proxy_node_ip": config.proxy_node_ip,
        "proxy_port": config.proxy_port,
        "sandbox_domain": config.sandbox_domain,
        "request_timeout": config.management_timeout_seconds,
    }


def input_files(root: Path):
    folder = root / "input"
    expected = {
        "context.json",
        "task.txt",
        *(f"{name}-schema.json" for name in ("prediction", "decision", "review")),
        *(f"prompt-{tf}.txt" for tf in ("1h", "4h", "6h", "12h", "review")),
        *(f"calculation-{tf}.json" for tf in ("1h", "4h", "6h", "12h")),
    }
    result, total = [], 0
    for path in sorted(folder.iterdir()):
        if path.name not in expected:
            raise DomainError("SANDBOX_INPUT_INVALID", "输入目录包含未授权文件", 503)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
                raise DomainError("SANDBOX_INPUT_INVALID", "输入文件类型或大小不符合限制", 503)
            data = handle.read(1_048_577)
        total += len(data)
        if total > 4_194_304:
            raise DomainError("SANDBOX_INPUT_INVALID", "任务输入总量超过限额", 503)
        result.append((path.name, data))
    return result


class CubeResources:
    """Control calls have explicit timeouts; attempt ownership precedes external creation."""

    def __init__(self, config, key, *, client=None):
        self.config = config
        self.sdk_settings = sdk_config(config.sandbox, key)
        self.key = key
        self.jobs = None
        self.http = client or httpx.AsyncClient(
            base_url=config.sandbox.base_url,
            trust_env=False,
            headers={"X-API-Key": key},
            timeout=httpx.Timeout(config.sandbox.management_timeout_seconds, connect=3),
        )
        self.records = {}
        self.bootstrap_files = {}

    async def request(self, method, path, payload=None, *, missing=False):
        try:
            async with asyncio.timeout(self.config.sandbox.management_timeout_seconds):
                response = await self.http.request(method, path, json=payload)
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            raise DomainError("CUBE_UNAVAILABLE", "Cube管理请求失败", 503) from exc

    async def ready(self):
        try:
            async with asyncio.timeout(1.8):
                health = await self.request("GET", "/health")
                await self.request("GET", "/sandboxes")
                unauthenticated = await self.http.get("/sandboxes", headers={"X-API-Key": ""})
                return health.get("status") == "ok" and unauthenticated.status_code == 401
        except (DomainError, httpx.HTTPError, TimeoutError):
            return False

    def metadata(self, attempt):
        return {
            "project": PROJECT,
            "job_id": str(attempt.job_id),
            "attempt_id": str(attempt.id),
            "fence": str(attempt.fence),
        }

    async def _record(self, attempt, values, *, expired=False):
        # Expired cleanup is a runtime maintenance write, still guarded by current ownership.
        async with self.jobs.database.transaction() as connection:
            if expired:
                await self.jobs.ownership.guard(connection)
            else:
                await self.jobs.guard(connection, attempt)
            await connection.execute(
                sa.update(s.job_attempts)
                .where(s.job_attempts.c.id == attempt.id)
                .values(sandbox_ref=values)
            )

    async def prepare(self, attempt, root):
        self.bootstrap_files[attempt.id] = root / ".cube.secrets.json"
        metadata = self.metadata(attempt)
        await self._record(attempt, {"status": "CREATING", "metadata": metadata})
        payload = {
            "templateID": self.config.sandbox.template,
            "timeout": self.config.sandbox.idle_timeout_seconds,
            "metadata": metadata,
            "envVars": {},
            "volumeMounts": [],
            "lifecycle": {"onTimeout": "kill", "autoResume": False},
            **sandbox_policy(self.config.sandbox, attempt.kind),
        }
        raw = await self.request("POST", "/sandboxes", payload)
        if not raw.get("sandboxID"):
            raise DomainError("CUBE_OWNERSHIP_INVALID", "Cube创建结果缺少沙箱ID", 503)
        # POST returns connection data without metadata. Persist the ID first, then
        # verify ownership through GET before uploading input or starting the Agent.
        raw = {**raw, "metadata": metadata}
        self.records[attempt.id] = raw
        # Persist the ID before setup; failures can then be reclaimed by the next runtime.
        await self._record(
            attempt, {"status": "PREPARING", "sandbox_id": raw["sandboxID"], "metadata": metadata}
        )
        info = await self.request("GET", "/sandboxes/" + quote(raw["sandboxID"], safe=""))
        limits = {
            "cpuCount": self.config.sandbox.cpu_count,
            "memoryMB": self.config.sandbox.memory_mib,
        }
        if ownership_metadata(info) != metadata or any(
            type(info.get(key)) is not int or not 0 < info[key] <= maximum
            for key, maximum in limits.items()
        ):
            raise DomainError("CUBE_RESOURCE_LIMITS", "沙箱归属或模板CPU/内存/磁盘限额未确认", 503)
        # CubeAPI 0.7.3 reports diskSizeMB=0 when the template disk size is unknown.
        # Never treat zero as proof of a bound: _populate measures the writable filesystem.
        disk = info.get("diskSizeMB")
        if type(disk) is not int or not 0 <= disk <= self.config.sandbox.disk_mib:
            raise DomainError("CUBE_RESOURCE_LIMITS", "沙箱磁盘限额未确认", 503)
        await asyncio.to_thread(self._populate, raw, root)
        settings = self.config.sandbox
        bootstrap = root / ".cube.secrets.json"
        fd = os.open(bootstrap, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                canonical_json(
                    {
                        "config": self.sdk_settings,
                        "sandbox": raw,
                        "settings": {
                            "execution_user": settings.execution_user,
                            "max_output_bytes": settings.max_output_bytes,
                            "max_processes": settings.max_processes,
                        },
                    }
                )
            )
        await self._record(
            attempt, {"status": "READY", "sandbox_id": raw["sandboxID"], "metadata": metadata}
        )

    def _populate(self, raw, root):
        from cubesandbox import Config, Sandbox

        CubeResources._secure_guest(self, raw)
        sandbox = Sandbox(raw, Config(**self.sdk_settings))
        user = self.config.sandbox.execution_user
        try:
            setup = sandbox.commands.run(
                "set -eu; test $(id -u " + shlex.quote(user) + ") -ne 0; "
                "mkdir -p /workspace/input /workspace/scratch; chown root:root /workspace; "
                "chmod 755 /workspace; chown root:root /workspace/input; "
                "chmod 700 /workspace/input; "
                "chmod 600 /var/log/envd.log; "
                "chown " + shlex.quote(user) + " /workspace/scratch; chmod 700 /workspace/scratch; "
                "printf 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6; "
                "printf 1 > /proc/sys/net/ipv6/conf/default/disable_ipv6",
                user="root",
                timeout=5,
            )
            if setup.exit_code != 0:
                raise DomainError(
                    "CUBE_TEMPLATE_INVALID", "模板普通用户、输入目录或IPv6限制未生效", 503
                )
            capacity = sandbox.commands.run(
                "python3 -c \"import os; s=os.statvfs('/workspace/scratch'); "
                'print((s.f_blocks*s.f_frsize+1048575)//1048576)"',
                user="root",
                timeout=5,
            )
            try:
                disk_mib = int(capacity.stdout.strip())
            except (TypeError, ValueError):
                disk_mib = 0
            if capacity.exit_code != 0 or not 0 < disk_mib <= self.config.sandbox.disk_mib:
                raise DomainError("CUBE_RESOURCE_LIMITS", "沙箱实际可写文件系统超过磁盘限额", 503)
            for name, data in input_files(root):
                sandbox.files.write("/workspace/input/" + name, data, user="root")
            result = sandbox.commands.run(
                "set -eu; chown -R root:root /workspace/input; "
                "chmod 444 /workspace/input/*; chmod 555 /workspace/input",
                user="root",
                timeout=5,
            )
            probe = sandbox.commands.run(
                "test $(id -u) -ne 0 && test ! -w /workspace && test ! -w /workspace/input "
                "&& test ! -w /workspace/input/context.json && test -w /workspace/scratch "
                "&& test $(cat /proc/sys/net/ipv6/conf/all/disable_ipv6) = 1 "
                "&& ! sudo -n true 2>/dev/null",
                user=user,
                cwd="/workspace/scratch",
                timeout=5,
            )
            if result.exit_code != 0 or probe.exit_code != 0:
                raise DomainError("CUBE_ISOLATION_FAILED", "任务基础权限隔离探针未通过", 503)
        finally:
            sandbox.close()

    def _secure_guest(self, raw):
        """Protect root envd before exposing task data or ordinary-user commands."""
        from cubesandbox import Config, Sandbox

        sandbox = Sandbox(raw, Config(**self.sdk_settings))
        headers = {
            "Host": sandbox.get_host(49983),
            "X-API-Key": self.sdk_settings["api_key"],
            "e2b-traffic-access-token": raw.get("trafficAccessToken", ""),
        }
        if raw.get("envdAccessToken"):
            headers["X-Access-Token"] = raw["envdAccessToken"]
        token = secrets.token_urlsafe(48)
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{self.config.sandbox.proxy_port}",
                trust_env=False,
                timeout=self.config.sandbox.management_timeout_seconds,
            ) as client:
                response = client.post(
                    "/init", headers=headers, json={"accessToken": token, "envVars": {}}
                )
                if response.status_code != 204:
                    raise DomainError("CUBE_GUEST_AUTH_FAILED", "guest管理认证初始化失败", 503)
                # An unauthenticated process RPC must fail before any model may run.
                public_headers = {k: v for k, v in headers.items() if k != "X-Access-Token"}
                probe = client.post("/process.Process/Start", headers=public_headers, content=b"")
                if probe.status_code != 401:
                    raise DomainError("CUBE_GUEST_AUTH_FAILED", "guest管理接口未强制认证", 503)
            raw["envdAccessToken"] = token
        except httpx.HTTPError as exc:
            raise DomainError("CUBE_GUEST_AUTH_FAILED", "guest管理认证请求失败", 503) from exc
        finally:
            sandbox.close()

    async def environment(self, attempt):
        root = (self.config.runtime.data_dir / "attempts" / str(attempt.id)).resolve()
        return {"TA_CUBE_SANDBOX_FILE": str(root / ".cube.secrets.json")}

    async def renew(self, attempt, remaining_seconds):
        """Refresh only an authorized attempt's sandbox; never extend its task deadline."""
        if not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise DomainError("AGENT_TIMEOUT", "研究任务总时限已用尽", 409)
        raw = self.records.get(attempt.id)
        if raw is None or raw.get("metadata") != self.metadata(attempt):
            raise DomainError("CUBE_OWNERSHIP_INVALID", "缺少本次任务的沙箱归属", 503)
        async with self.jobs.database.transaction() as connection:
            job, record, now = await self.jobs.guard(connection, attempt)
            limits = [remaining_seconds, (record["token_expires_at"] - now).total_seconds()]
            if job["deadline_at"] is not None:
                limits.append((job["deadline_at"] - now).total_seconds())
            # A coordinator includes child preparation, parallel analysis and
            # bounded retries. Its durable round/token deadlines govern renewal;
            # each role's 120s execution deadline is supervised separately.
            coordinator = attempt.payload.get("analysis_round") and not attempt.payload.get(
                "analysis_managed"
            )
            if record["model_started_at"] is not None and not coordinator:
                limits.append(
                    (
                        record["model_started_at"]
                        + timedelta(seconds=self.config.runtime.agent_timeout_seconds)
                        - now
                    ).total_seconds()
                )
        remaining = min(limits)
        if remaining <= 0:
            raise DomainError("AGENT_TIMEOUT", "研究任务总时限已用尽", 409)
        ident = quote(raw["sandboxID"], safe="")
        current = await self.request("GET", "/sandboxes/" + ident)
        if ownership_metadata(current) != raw["metadata"]:
            raise DomainError("CUBE_OWNERSHIP_INVALID", "沙箱归属已变化，拒绝续期", 503)
        timeout = min(
            self.config.sandbox.idle_timeout_seconds,
            math.ceil(remaining + self.config.sandbox.timeout_grace_seconds),
        )
        # No database transaction is held during control-plane I/O. Cube rebases the
        # idle clock to now; a shorter remaining task budget produces a shorter TTL.
        await self.request("POST", "/sandboxes/" + ident + "/timeout", {"timeout": timeout})
        await self._record(
            attempt,
            {
                "status": "READY",
                "sandbox_id": raw["sandboxID"],
                "metadata": raw["metadata"],
                "timeout_seconds": timeout,
            },
        )
        attempt_event("sandbox_timeout_updated", attempt, timeout_seconds=timeout)

    async def _destroy(self, raw):
        ident = quote(raw["sandboxID"], safe="")
        # Verify metadata again: never delete an arbitrary ID found in a mutable local file.
        current = await self.request("GET", "/sandboxes/" + ident, missing=True)
        if current is None:
            return True
        if ownership_metadata(current) != ownership_metadata(raw):
            raise DomainError("CUBE_OWNERSHIP_INVALID", "沙箱归属已变化，拒绝回收", 503)
        await self.request("DELETE", "/sandboxes/" + ident, missing=True)
        return await self.request("GET", "/sandboxes/" + ident, missing=True) is None

    async def cleanup(self, attempt):
        raw = self.records.pop(attempt.id, None)
        if raw is None:
            async with self.jobs.database.engine.connect() as connection:
                record = await connection.scalar(
                    sa.select(s.job_attempts.c.sandbox_ref).where(s.job_attempts.c.id == attempt.id)
                )
            if not record or record.get("status") == "CLEANED":
                if record:
                    await self._clear_bootstrap(attempt)
                return True
            if not record.get("sandbox_id"):
                # Recovery searches metadata again, including responses arriving after a timeout.
                return False
            raw = {"sandboxID": record["sandbox_id"], "metadata": record["metadata"]}
        try:
            cleaned = await self._destroy(raw)
            await self._record(
                attempt,
                {
                    "status": "CLEANED" if cleaned else "CLEANUP_PENDING",
                    "sandbox_id": raw["sandboxID"],
                    "metadata": raw["metadata"],
                },
                expired=True,
            )
            if cleaned:
                await self._clear_bootstrap(attempt)
            return cleaned
        except DomainError:
            return False

    async def _clear_bootstrap(self, attempt):
        # Only confirmed remote cleanup permits a fresh launch's local secrets.
        # Keep O_EXCL in prepare: an unconfirmed old sandbox cannot be overwritten.
        path = self.bootstrap_files.get(
            attempt.id,
            self.config.runtime.data_dir / "attempts" / str(attempt.id) / ".cube.secrets.json",
        )
        await asyncio.to_thread(path.unlink, missing_ok=True)
        self.bootstrap_files.pop(attempt.id, None)

    async def recover(self):
        sandboxes = await self.request("GET", "/sandboxes")
        for raw in sandboxes:
            metadata = ownership_metadata(raw)
            if metadata.get("project") != PROJECT:
                continue
            async with self.jobs.database.transaction() as connection:
                await self.jobs.ownership.guard(connection)
                row = (
                    (
                        await connection.execute(
                            sa.select(
                                s.job_attempts,
                                s.jobs.c.state,
                                s.jobs.c.fence.label("current_fence"),
                                s.jobs.c.lease_until,
                            )
                            .join(s.jobs, s.jobs.c.id == s.job_attempts.c.job_id)
                            .where(
                                sa.cast(s.job_attempts.c.id, sa.Text)
                                == metadata.get("attempt_id", "")
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                now = await self.jobs.database.now(connection)
                if row is None or metadata != {
                    "project": PROJECT,
                    "job_id": str(row["job_id"]),
                    "attempt_id": str(row["id"]),
                    "fence": str(row["fence"]),
                }:
                    continue
                active = (
                    row["status"] == "RUNNING"
                    and row["state"] == "RUNNING"
                    and row["fence"] == row["current_fence"]
                    and row["lease_until"] > now
                    and row["runtime_instance_id"] == self.jobs.ownership.instance_id
                )
            if active:
                continue
            cleaned = await self._destroy(raw)
            async with self.jobs.database.transaction() as connection:
                await self.jobs.ownership.guard(connection)
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == row["id"])
                    .values(
                        sandbox_ref={
                            "status": "CLEANED" if cleaned else "CLEANUP_PENDING",
                            "sandbox_id": raw["sandboxID"],
                            "metadata": metadata,
                        }
                    )
                )

    async def close(self):
        await self.http.aclose()
