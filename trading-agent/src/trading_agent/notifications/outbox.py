from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.config import AppConfig
from trading_agent.domain.serialization import content_hash
from trading_agent.notifications.routing import destination
from trading_agent.runtime.processes import ManagedProcess, safe_environment
from trading_agent.storage import schema as s

logger = logging.getLogger(__name__)

CONTEXT_REFRESH_CODES = {"CONTEXT_EXPIRED", "CONTEXT_CHANGED", "POSITION_CHANGED"}
DELIVERY_CONCURRENCY = 4


def is_context_refresh(kind, code):
    return kind in {"PREDICT_PARENT", "PREDICT_TIMEFRAME"} and code in CONTEXT_REFRESH_CODES


async def enqueue(connection, *, key, target, body, now, include_event_id=True) -> UUID | None:
    if target is None:
        return None
    event_id = content_hash(key)[:12]
    complete = f"{body}\n[event:{event_id}]" if include_event_id else body
    ident = await connection.scalar(
        insert(s.outbox)
        .values(
            event_key=key,
            target=target,
            body=complete,
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(index_elements=[s.outbox.c.event_key])
        .returning(s.outbox.c.id)
    )
    if ident:
        return ident
    row = (
        (await connection.execute(sa.select(s.outbox).where(s.outbox.c.event_key == key)))
        .mappings()
        .one()
    )
    if row["target"] != target or row["body"] != complete:
        raise ValueError("Notification event key has conflicting immutable content")
    return row["id"]


@dataclass(frozen=True)
class Delivery:
    state: str
    message_id: str | None = None
    error_code: str | None = None


class HermesSender:
    def __init__(self, config: AppConfig):
        self.config = config

    async def send(self, target: str, body: str) -> Delivery:
        hermes = self.config.hermes
        if hermes.executable is None:
            return Delivery("FAILED", error_code="NOTIFICATION_NOT_CONFIGURED")
        try:
            profile, target = destination(self.config, target)
        except ValueError as error:
            return Delivery("FAILED", error_code=str(error))
        env = safe_environment()
        if hermes.home:
            env["HERMES_HOME"] = str(hermes.home.resolve())
        started = monotonic()
        try:
            process = await ManagedProcess.start(
                [
                    str(hermes.executable),
                    "-p",
                    profile,
                    "send",
                    "--to",
                    target,
                    "--file",
                    "-",
                    "--json",
                ],
                env=env,
                input_bytes=body.encode(),
                kill_grace=self.config.runtime.kill_grace_seconds,
            )
        except (FileNotFoundError, PermissionError):
            # No child exists, so no remote delivery could have happened.
            return Delivery("FAILED", error_code="SEND_PROCESS_START_FAILED")
        except OSError:
            return Delivery("UNKNOWN", error_code="SEND_START_UNCONFIRMED")
        try:
            remaining = self.config.notification.send_timeout_seconds - (monotonic() - started)
            result = await process.wait(max(0, remaining))
        except TimeoutError:
            return Delivery("UNKNOWN", error_code="SEND_TIMEOUT")
        except OSError:
            return Delivery("UNKNOWN", error_code="SEND_PROCESS_ERROR")
        if result.truncated:
            return Delivery("UNKNOWN", error_code="UNRECOGNIZED_SEND_RESPONSE")
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            return Delivery("UNKNOWN", error_code="UNRECOGNIZED_SEND_RESPONSE")
        if (
            result.returncode == 0
            and isinstance(payload, dict)
            and payload.get("success") is True
            and not payload.get("error")
            and not payload.get("skipped")
            and not payload.get("partial_success")
        ):
            message_id = payload.get("message_id")
            return Delivery("SENT", message_id=str(message_id) if message_id is not None else None)
        if isinstance(payload, dict) and (payload.get("error") or payload.get("partial_success")):
            # Hermes' untyped error can mean a lost receipt or partial chunk send.
            # It does not prove that the remote rejected the whole message.
            return Delivery("UNKNOWN", error_code="SEND_UNCONFIRMED")
        return Delivery("UNKNOWN", error_code="UNRECOGNIZED_SEND_RESPONSE")


class Notifier:
    def __init__(self, database, ownership, sender, config: AppConfig, *, on_progress=None):
        self.database, self.ownership, self.sender, self.config = (
            database,
            ownership,
            sender,
            config,
        )
        self.semaphore = asyncio.Semaphore(DELIVERY_CONCURRENCY)
        self.on_progress = on_progress or (lambda: None)

    async def claim(self):
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            # The prediction channel now carries parent summaries and errors only.
            # Retire unsent notices from the old policy; preserve uncertain and
            # confirmed deliveries, their payloads and attempt history.
            await connection.execute(
                sa.update(s.outbox)
                .where(
                    sa.or_(
                        s.outbox.c.event_key.startswith("prediction-ended:"),
                        s.outbox.c.event_key.startswith("analysis-recovered:"),
                    ),
                    s.outbox.c.status.in_(("PENDING", "RETRY_WAIT")),
                )
                .values(status="FAILED", last_error="NOTIFICATION_DISABLED")
            )
            # Retire queued notices from older runtimes without deleting their
            # event/body/attempt history. Context refresh is automatic recovery.
            obsolete = sa.select(
                sa.literal("job-failed:")
                + sa.cast(s.jobs.c.id, sa.String)
                + ":"
                + s.jobs.c.last_error["code"].as_string()
            ).where(
                s.jobs.c.kind.in_(("PREDICT_PARENT", "PREDICT_TIMEFRAME")),
                s.jobs.c.last_error["code"].as_string().in_(CONTEXT_REFRESH_CODES),
            )
            await connection.execute(
                sa.update(s.outbox)
                .where(
                    s.outbox.c.event_key.in_(obsolete),
                    sa.or_(
                        s.outbox.c.status.in_(("PENDING", "RETRY_WAIT", "UNKNOWN")),
                        sa.and_(s.outbox.c.status == "SENDING", s.outbox.c.lease_until <= now),
                    ),
                )
                .values(
                    status="FAILED",
                    last_error="NOTIFICATION_SUPERSEDED",
                    claim_id=None,
                    lease_until=None,
                )
            )
            # An expired sender lease cannot tell us whether the remote accepted
            # the message. Preserve UNKNOWN for inspection; never resend blindly.
            await connection.execute(
                sa.update(s.outbox)
                .where(s.outbox.c.status == "SENDING", s.outbox.c.lease_until <= now)
                .values(
                    status="UNKNOWN",
                    last_error="PRIOR_DELIVERY_UNKNOWN",
                    claim_id=None,
                    lease_until=None,
                )
            )
            rows = (
                (
                    await connection.execute(
                        sa.select(s.outbox)
                        .where(
                            s.outbox.c.status.in_(("PENDING", "RETRY_WAIT")),
                            s.outbox.c.next_attempt_at <= now,
                        )
                        .order_by(s.outbox.c.next_attempt_at)
                        .with_for_update(skip_locked=True)
                        # Do not lease messages that will wait behind other sends.
                        .limit(DELIVERY_CONCURRENCY)
                    )
                )
                .mappings()
                .all()
            )
            claims = []
            for row in rows:
                if row["attempts"] >= self.config.notification.attempts:
                    await connection.execute(
                        sa.update(s.outbox)
                        .where(s.outbox.c.id == row["id"])
                        .values(
                            status="FAILED",
                            last_error="ATTEMPTS_EXHAUSTED",
                            claim_id=None,
                            lease_until=None,
                        )
                    )
                    continue
                claim = uuid4()
                await connection.execute(
                    sa.update(s.outbox)
                    .where(s.outbox.c.id == row["id"])
                    .values(
                        status="SENDING",
                        attempts=row["attempts"] + 1,
                        claim_id=claim,
                        lease_until=now
                        + timedelta(seconds=self.config.notification.claim_lease_seconds),
                    )
                )
                claims.append({**dict(row), "claim_id": claim, "attempts": row["attempts"] + 1})
            return claims

    async def acknowledge(self, claim, delivery: Delivery) -> bool:
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            row = (
                (
                    await connection.execute(
                        sa.select(s.outbox).where(s.outbox.c.id == claim["id"]).with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = await self.database.now(connection)
            if (
                row["status"] != "SENDING"
                or row["claim_id"] != claim["claim_id"]
                or row["lease_until"] <= now
            ):
                return False
            state = delivery.state
            if state not in {"SENT", "FAILED", "UNKNOWN"}:
                raise ValueError("Unknown transport result")
            if state == "FAILED" and row["attempts"] < self.config.notification.attempts:
                state = "RETRY_WAIT"
            delays = self.config.notification.retry_seconds
            await connection.execute(
                sa.update(s.outbox)
                .where(s.outbox.c.id == row["id"])
                .values(
                    status=state,
                    claim_id=None,
                    lease_until=None,
                    transport_message_id=delivery.message_id,
                    last_error=delivery.error_code,
                    next_attempt_at=now
                    + timedelta(seconds=delays[min(row["attempts"] - 1, len(delays) - 1)]),
                )
            )
        logger.info(
            "notification_acknowledged",
            extra={
                "outbox_id": str(claim["id"]),
                "claim_id": str(claim["claim_id"]),
                "attempt_no": claim["attempts"],
                "delivery_status": state,
                "error_code": delivery.error_code,
            },
        )
        return True

    async def _deliver(self, claim):
        async with self.semaphore:
            async with self.database.transaction() as connection:
                await self.ownership.guard(connection)
                now = await self.database.now(connection)
                current = await connection.scalar(
                    sa.select(s.outbox.c.id).where(
                        s.outbox.c.id == claim["id"],
                        s.outbox.c.status == "SENDING",
                        s.outbox.c.claim_id == claim["claim_id"],
                        s.outbox.c.lease_until > now,
                    )
                )
            if current is None:
                return
            try:
                # The sender owns its deadline. Leave bounded process cleanup time
                # before this outer guard; equal nested timers discarded valid receipts.
                cleanup = 2 * self.config.runtime.kill_grace_seconds
                async with asyncio.timeout(self.config.notification.send_timeout_seconds + cleanup):
                    result = await self.sender.send(claim["target"], claim["body"])
            except TimeoutError:
                result = Delivery("UNKNOWN", error_code="SEND_TIMEOUT")
            await self.acknowledge(claim, result)
            self.on_progress()

    async def tick(self):
        claims = await self.claim()
        await asyncio.gather(*(self._deliver(claim) for claim in claims))
