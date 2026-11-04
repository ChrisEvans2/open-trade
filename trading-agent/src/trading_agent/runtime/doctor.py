from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from pathlib import Path

import httpx

from trading_agent.notifications.outbox import HermesSender


async def check_runtime(config, token):
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{config.runtime.api_port}",
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
        timeout=httpx.Timeout(3, connect=1),
    ) as client:
        try:
            response = await client.get("/readyz")
            data = response.json()
            if not isinstance(data, dict):
                return {"ready": False, "code": "DOCTOR_RESPONSE_INVALID"}
            if response.status_code == 200 and data.get("ready") is True:
                return {"ready": True, "code": "READY", "checks": data.get("checks", {})}
            if response.status_code in {401, 403}:
                return {"ready": False, "code": "DOCTOR_CREDENTIAL_REJECTED"}
            if response.status_code == 503:
                return {
                    "ready": False,
                    "code": "RUNTIME_NOT_READY",
                    "checks": data.get("checks", {}),
                }
            return {"ready": False, "code": "DOCTOR_RESPONSE_INVALID"}
        except (httpx.HTTPError, ValueError):
            return {"ready": False, "code": "RUNTIME_UNREACHABLE"}


async def doctor(config, token, *, notify=False, sender=None, state_path=None):
    result = await check_runtime(config, token)
    if not notify:
        return result
    if not config.notification.target:
        return {**result, "notification": "TARGET_NOT_CONFIGURED"}
    path = state_path or config.runtime.data_dir.resolve() / "doctor-state.json"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(Path(str(path) + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, "notification": "CHECK_ALREADY_RUNNING"}
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            previous = {}
        code, now = result["code"], time.time()
        last_attempts = previous.get("last_attempts", {})
        should_send = (code == "READY" and previous.get("incident", False)) or (
            code != "READY" and now - last_attempts.get(code, 0) >= 86400
        )
        state = {
            "incident": previous.get("incident", False) or code != "READY",
            "last_attempts": last_attempts,
            "code": code,
        }
        if should_send:
            # Reserve locally before I/O: a crash cannot cause Cron to flood the same incident.
            state["last_attempts"][code] = now
            _save(path, state)
            body = (
                "交易研究runtime已恢复。"
                if code == "READY"
                else f"交易研究runtime异常：{code}。请运行ta doctor检查。"
            )
            delivery = await (sender or HermesSender(config)).send(config.notification.target, body)
            result["notification"] = delivery.state
            if code == "READY":
                # One recovery notice per incident, including UNKNOWN (possibly delivered).
                state["incident"] = False
        else:
            result["notification"] = "QUIET"
        _save(path, state)
    return result


def _save(path, state):
    temporary = Path(str(path) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def doctor_sync(config, token, notify=False):
    return asyncio.run(doctor(config, token, notify=notify))
