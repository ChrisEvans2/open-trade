"""Owned, durable sampling of OKX's incomplete public liquidation feed."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import sqlalchemy as sa
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from trading_agent.domain.errors import StaleAttempt
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import INTERVAL_SECONDS, TIMEFRAMES, boundary
from trading_agent.market.models import Observation
from trading_agent.market.observations import number, timestamp
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_observation

RAW_PREFIX = "okx_liquidation_raw:"
STREAM_SOURCE = "okx_liquidation_stream:v1"
WINDOW_PREFIX = "okx_liquidation_window:"
MAX_EVENTS = 10000
MAX_SEGMENTS = 6000


def parse_frame(message, config, available_at):
    """No venue event ID exists: deduplicate identical details by their canonical content."""
    kind = "SWAP" if config.contract_type == "perpetual" else "FUTURES"
    if (
        not isinstance(message, dict)
        or message.get("arg") != {"channel": "liquidation-orders", "instType": kind}
        or not isinstance(message.get("data"), list)
        or len(message["data"]) > 1000
    ):
        raise ValueError("无效公开清算消息")
    result = {}
    count = 0
    for item in message["data"]:
        if not isinstance(item, dict):
            raise ValueError("无效清算标的")
        if item.get("instId") != config.inst_id:
            continue
        if item.get("instType") != kind or not isinstance(item.get("details"), list):
            raise ValueError("清算标的范围不符")
        for detail in item["details"]:
            count += 1
            if count > 1000 or not isinstance(detail, dict):
                raise ValueError("清算明细超限或无效")
            side, position = detail.get("side"), detail.get("posSide")
            if side not in {"buy", "sell"} or position not in {"long", "short", "net"}:
                raise ValueError("清算方向无效")
            # A sell closes a long; a buy closes a short, including net position mode.
            liquidated = "long" if side == "sell" else "short"
            if position != "net" and position != liquidated:
                raise ValueError("清算持仓与成交方向矛盾")
            quantity, price = number(detail["sz"]), number(detail["bkPx"])
            event_at = timestamp(detail["ts"])
            if quantity <= 0 or price <= 0 or event_at > available_at:
                raise ValueError("清算数量、价格或时间无效")
            payload = {
                "record": "EVENT",
                "coverage": "public_sample_events",
                "inst_type": kind,
                "side": side,
                "pos_side": position,
                "liquidated_side": liquidated,
                "contracts": quantity,
                "bankruptcy_price": price,
                "bankruptcy_loss": number(detail["bkLoss"]),
                "ccy": detail.get("ccy", ""),
                "venue_timestamp": str(int(detail["ts"])),
            }
            identity = content_hash({"inst_id": config.inst_id, **payload})
            result[identity] = Observation(
                source=RAW_PREFIX + identity,
                inst_id=config.inst_id,
                kind="liquidation",
                event_at=event_at,
                available_at=available_at,
                unit="contracts",
                payload=payload,
            )
    return tuple(result.values())


def uncovered(start, end, segments):
    """Clip and union confirmed connection segments; gaps are never filled by silence."""
    cursor, gaps = start, []
    for left, right in sorted(segments):
        left, right = max(start, left), min(end, right)
        if right <= left or right <= cursor:
            continue
        if left > cursor:
            gaps.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def contract_metadata(instrument, config):
    """Official notional formula; unsupported specifications remain missing, never guessed."""
    kind = "SWAP" if config.contract_type == "perpetual" else "FUTURES"
    parts = config.inst_id.split("-")
    if (
        not isinstance(instrument, dict)
        or len(parts) != 3
        or instrument.get("instId") != config.inst_id
        or instrument.get("instType") != kind
        or instrument.get("instFamily") != "-".join(parts[:2])
        or instrument.get("settleCcy") != config.settlement_asset
        or instrument.get("state") != "live"
        or number(instrument["ctMult"]) != 1
    ):
        raise ValueError("清算金额缺少已验证合约元数据")
    face = number(instrument["ctVal"])
    contract_type = instrument.get("ctType")
    currency = instrument.get("ctValCcy")
    if face <= 0 or not (
        contract_type == "linear"
        and currency == parts[0]
        or contract_type == "inverse"
        and currency == parts[1] == "USD"
    ):
        raise ValueError("清算金额合约面值口径未支持")
    return {
        "source": "okx:/api/v5/public/instruments",
        "inst_id": config.inst_id,
        "contract_type": contract_type,
        "face_value": face,
        "face_currency": currency,
        "multiplier": Decimal(1),
        "quote_currency": parts[1],
    }


def is_port_notice(message):
    return (
        isinstance(message, dict)
        and message.get("event") == "notice"
        and message.get("code") == "64010"
    )


class LiquidationSampler:
    def __init__(
        self, database, ownership, config, *, connector=connect, on_fatal=None, inst_ids=None
    ):
        self.database, self.ownership, self.config = database, ownership, config
        self.connector, self.on_fatal = connector, on_fatal
        self.configs = tuple(
            config.model_copy(update={"inst_id": inst}) for inst in (inst_ids or (config.inst_id,))
        )
        self.task = None
        self.cursor = self.confirmed = self.session_id = None
        self.connected = False
        self.failure_code = None
        self.heartbeat_seconds, self.pong_seconds = 20, 5

    async def start(self):
        self.config.require_instrument()
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            row = (
                (
                    await connection.execute(
                        sa.select(s.market_observations)
                        .where(
                            s.market_observations.c.source == STREAM_SOURCE,
                            s.market_observations.c.inst_id == self.config.inst_id,
                        )
                        .order_by(s.market_observations.c.available_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row:
                self.cursor = row["coverage_end"] or row["event_at"]
        await self._gap("RUNTIME_RESTART")
        self.task = asyncio.create_task(self.run(), name="public-liquidations")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            # Cancellation may interrupt the disconnect transaction before it commits.
            try:
                await self._gap("SHUTDOWN")
            except (StaleAttempt, sa.exc.SQLAlchemyError):
                pass

    async def _marker(self, record, reason=None):
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            start = self.confirmed if record == "COVERAGE" else self.cursor
            if record == "GAP" and (start is None or start >= now):
                return
            if record == "COVERAGE" and (start is None or start >= now):
                return
            for scoped in self.configs:
                await persist_observation(
                    connection,
                    Observation(
                        source=STREAM_SOURCE,
                        inst_id=scoped.inst_id,
                        kind="liquidation",
                        event_at=now,
                        available_at=now,
                        coverage_start=start if record != "OPEN" else None,
                        coverage_end=now if record != "OPEN" else None,
                        unit="connection_seconds",
                        quality="PARTIAL_COVERAGE" if record == "GAP" else "OK",
                        payload={
                            "record": record,
                            "session_id": str(self.session_id),
                            "reason": reason,
                        },
                    ),
                )
        self.cursor = now
        if record in {"OPEN", "COVERAGE"}:
            self.confirmed = now

    async def _gap(self, reason):
        await self._marker("GAP", reason)

    async def record_frame(self, message):
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            for scoped in self.configs:
                for event in parse_frame(message, scoped, now):
                    await persist_observation(connection, event)

    async def _session(self, socket):
        kind = "SWAP" if self.config.contract_type == "perpetual" else "FUTURES"
        argument = {"channel": "liquidation-orders", "instType": kind}
        await socket.send(json.dumps({"id": "liquidations", "op": "subscribe", "args": [argument]}))
        async with asyncio.timeout(5):
            while True:
                acknowledgement = json.loads(await socket.recv())
                # Port 8443 sends this notice before the subscription ACK. It
                # does not confirm subscription and must not open coverage.
                if is_port_notice(acknowledgement):
                    continue
                break
        if (
            not isinstance(acknowledgement, dict)
            or acknowledgement.get("event") != "subscribe"
            or acknowledgement.get("arg") != argument
            or acknowledgement.get("id") != "liquidations"
        ):
            raise ValueError("公开清算订阅未确认")
        self.session_id = uuid4()
        await self._gap("RECONNECT")
        await self._marker("OPEN")
        self.connected, self.failure_code = True, None
        next_ping, pong_deadline = time.monotonic() + self.heartbeat_seconds, None
        while True:
            now = time.monotonic()
            if pong_deadline is not None and now >= pong_deadline:
                raise TimeoutError("清算频道心跳未确认")
            if pong_deadline is None and now >= next_ping:
                await socket.send("ping")
                pong_deadline = time.monotonic() + self.pong_seconds
            deadline = pong_deadline if pong_deadline is not None else next_ping
            try:
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    message = await socket.recv()
            except TimeoutError:
                if pong_deadline is not None:
                    raise
                continue
            if message == "pong":
                if pong_deadline is not None:
                    await self._marker("COVERAGE")
                    pong_deadline, next_ping = None, time.monotonic() + self.heartbeat_seconds
                continue
            frame = json.loads(message)
            if not is_port_notice(frame):
                await self.record_frame(frame)

    async def run(self):
        backoff = 1
        try:
            while True:
                try:
                    async with self.connector(
                        self.config.public_ws_url,
                        open_timeout=3,
                        close_timeout=1,
                        ping_interval=None,
                        max_size=1_048_576,
                        max_queue=16,
                    ) as socket:
                        await self._session(socket)
                except (OSError, WebSocketException, TimeoutError, ValueError, KeyError, TypeError):
                    self.failure_code = "LIQUIDATION_SAMPLE_DISCONNECTED"
                except sa.exc.SQLAlchemyError:
                    self.failure_code = "LIQUIDATION_SAMPLE_DATABASE_UNAVAILABLE"
                finally:
                    self.connected = False
                    try:
                        await self._gap("DISCONNECTED")
                    except sa.exc.SQLAlchemyError:
                        pass  # The next start records the gap from the last durable checkpoint.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        except StaleAttempt:
            self.failure_code = "RUNTIME_OWNERSHIP_LOST"
            if self.on_fatal:
                self.on_fatal(self.failure_code)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure_code = "LIQUIDATION_SAMPLER_CRASHED"
            if self.on_fatal:
                self.on_fatal(self.failure_code)

    async def windows(
        self,
        connection,
        anchor,
        decision_at,
        *,
        instrument=None,
        inst_id=None,
        timeframes=TIMEFRAMES,
    ):
        """Bounded queries inside the already fenced snapshot transaction; no network I/O."""
        await self.ownership.guard(connection)
        scoped = self.config.model_copy(update={"inst_id": inst_id}) if inst_id else self.config
        try:
            metadata = contract_metadata(instrument, scoped)
        except (KeyError, ValueError, TypeError):
            metadata = None
        result = []
        table = s.market_observations
        for timeframe in timeframes:
            end = boundary(anchor, timeframe)
            start = end - timedelta(seconds=INTERVAL_SECONDS[timeframe])
            events = (
                (
                    await connection.execute(
                        sa.select(table)
                        .where(
                            table.c.inst_id == scoped.inst_id,
                            table.c.source.startswith(RAW_PREFIX, autoescape=True),
                            table.c.event_at >= start,
                            table.c.event_at < end,
                            table.c.available_at <= decision_at,
                        )
                        .order_by(table.c.event_at, table.c.source, table.c.revision_no)
                        .limit(MAX_EVENTS + 1)
                    )
                )
                .mappings()
                .all()
            )
            coverage = (
                (
                    await connection.execute(
                        sa.select(table)
                        .where(
                            table.c.inst_id == scoped.inst_id,
                            table.c.source == STREAM_SOURCE,
                            table.c.payload["record"].astext == "COVERAGE",
                            table.c.coverage_start < end,
                            table.c.coverage_end > start,
                            table.c.available_at <= decision_at,
                        )
                        .order_by(table.c.coverage_start, table.c.available_at)
                        .limit(MAX_SEGMENTS + 1)
                    )
                )
                .mappings()
                .all()
            )
            truncated = len(events) > MAX_EVENTS or len(coverage) > MAX_SEGMENTS
            gaps = uncovered(
                start, end, [(r["coverage_start"], r["coverage_end"]) for r in coverage]
            )
            longs = sum(
                (
                    Decimal(r["payload"]["contracts"])
                    for r in events
                    if r["payload"]["liquidated_side"] == "long"
                ),
                Decimal(0),
            )
            shorts = sum(
                (
                    Decimal(r["payload"]["contracts"])
                    for r in events
                    if r["payload"]["liquidated_side"] == "short"
                ),
                Decimal(0),
            )

            def notional(row):
                raw = row["payload"]
                return (
                    Decimal(raw["contracts"])
                    * metadata["face_value"]
                    * (
                        Decimal(raw["bankruptcy_price"])
                        if metadata["contract_type"] == "linear"
                        else Decimal(1)
                    )
                )

            payload = {
                "record": "WINDOW",
                "coverage": "public_sample",
                "timeframe": timeframe,
                "long_contracts": longs,
                "short_contracts": shorts,
                "instrument_spec": metadata,
                "long_quote": sum(
                    (notional(r) for r in events if r["payload"]["liquidated_side"] == "long"),
                    Decimal(0),
                )
                if metadata
                else None,
                "short_quote": sum(
                    (notional(r) for r in events if r["payload"]["liquidated_side"] == "short"),
                    Decimal(0),
                )
                if metadata
                else None,
                "event_ids": [str(r["id"]) for r in events],
                "coverage_ids": [str(r["id"]) for r in coverage],
                "gaps": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in gaps],
                "truncated": truncated,
                "missing_reason": "UNVERIFIED_CONTRACT_NOTIONAL"
                if metadata is None
                else "SAMPLE_QUERY_LIMIT"
                if truncated
                else "NO_SAMPLED_EVENTS"
                if not events
                else None,
            }
            result.append(
                await persist_observation(
                    connection,
                    Observation(
                        source=f"{WINDOW_PREFIX}{timeframe}:v2",
                        inst_id=scoped.inst_id,
                        kind="liquidation",
                        event_at=end,
                        available_at=decision_at,
                        coverage_start=start,
                        coverage_end=end,
                        unit=f"quote_{metadata['quote_currency']}"
                        if metadata
                        else "unverified_quote_notional",
                        quality="MISSING" if metadata is None or truncated or not events else "OK",
                        payload=payload,
                    ),
                )
            )
        return tuple(result)
