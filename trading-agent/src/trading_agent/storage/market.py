from __future__ import annotations

from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.market.models import Bar, Observation
from trading_agent.market.snapshots import Snapshot
from trading_agent.storage import schema as s


def content_lock_key(key: str):
    number = int(content_hash(key)[:16], 16)
    if number >= 2**63:
        number -= 2**64
    return number


async def content_lock(connection: AsyncConnection, key: str):
    # A transaction-scoped lock serializes the first insertion and revision allocation.
    await connection.execute(sa.select(sa.func.pg_advisory_xact_lock(content_lock_key(key))))


def row_bar(row) -> Bar:
    return Bar(
        id=row["id"],
        source=row["source"],
        inst_id=row["inst_id"],
        timeframe=row["interval"],
        open_at=row["open_at"],
        close_boundary=row["close_boundary"],
        open=row["o"],
        high=row["h"],
        low=row["l"],
        close=row["c"],
        volume_contracts=row["volume_contracts"],
        volume_base=row["volume_base"],
        turnover_quote=row["turnover_quote"],
        available_at=row["available_at"],
        revision_no=row["revision_no"],
    )


async def persist_bar(connection: AsyncConnection, bar: Bar, symbol="BTCUSDT") -> Bar:
    await content_lock(connection, f"bar:{bar.source}:{bar.inst_id}:{bar.timeframe}:{bar.open_at}")
    query = (
        sa.select(s.market_bars)
        .where(
            s.market_bars.c.source == bar.source,
            s.market_bars.c.inst_id == bar.inst_id,
            s.market_bars.c.interval == bar.timeframe,
            s.market_bars.c.open_at == bar.open_at,
        )
        .order_by(s.market_bars.c.revision_no.desc())
        .limit(1)
    )
    previous = (await connection.execute(query)).mappings().first()
    if previous and previous["payload_hash"] == bar.market_content_hash:
        return row_bar(previous)
    revision = previous["revision_no"] + 1 if previous else 1
    stored = bar.model_copy(update={"revision_no": revision, "id": uuid4() if previous else bar.id})
    await connection.execute(
        insert(s.market_bars).values(
            id=stored.id,
            source=stored.source,
            symbol=symbol,
            inst_id=stored.inst_id,
            interval=stored.timeframe,
            open_at=stored.open_at,
            close_boundary=stored.close_boundary,
            o=stored.open,
            h=stored.high,
            l=stored.low,
            c=stored.close,
            volume_contracts=stored.volume_contracts,
            volume_base=stored.volume_base,
            turnover_quote=stored.turnover_quote,
            available_at=stored.available_at,
            revision_no=revision,
            payload_hash=stored.market_content_hash,
        )
    )
    return stored


async def persist_bars(connection: AsyncConnection, bars, symbol="BTCUSDT"):
    """Persist a window in three round trips with the existing per-bar revision locks."""
    bars = tuple(bars)
    if not bars:
        return ()
    keys = [(b.source, b.inst_id, b.timeframe, b.open_at) for b in bars]
    if len(set(keys)) != len(keys) or len(keys) > 1000:
        raise ValueError("行情批次包含重复身份或超过1000根")
    # Ascending identity order matches single-window persist_bar writers.
    lock_rows = [
        (index, content_lock_key(f"bar:{src}:{inst}:{tf}:{opened}"))
        for index, (src, inst, tf, opened) in enumerate(sorted(keys))
    ]
    locks = (
        sa.values(sa.column("ordinal", sa.Integer), sa.column("key", sa.BigInteger))
        .data(lock_rows)
        .alias("bar_locks")
    )
    await connection.execute(
        sa.select(sa.func.pg_advisory_xact_lock(locks.c.key)).order_by(locks.c.ordinal)
    )
    columns = (
        s.market_bars.c.source,
        s.market_bars.c.inst_id,
        s.market_bars.c.interval,
        s.market_bars.c.open_at,
    )
    rows = (
        (
            await connection.execute(
                sa.select(s.market_bars)
                .where(sa.tuple_(*columns).in_(keys))
                .distinct(*columns)
                .order_by(*columns, s.market_bars.c.revision_no.desc())
            )
        )
        .mappings()
        .all()
    )
    previous = {(r["source"], r["inst_id"], r["interval"], r["open_at"]): r for r in rows}
    result, inserts = [], []
    for bar, key in zip(bars, keys, strict=True):
        old = previous.get(key)
        if old and old["payload_hash"] == bar.market_content_hash:
            result.append(row_bar(old))
            continue
        stored = bar.model_copy(
            update={
                "revision_no": old["revision_no"] + 1 if old else 1,
                "id": uuid4() if old else bar.id,
            }
        )
        result.append(stored)
        inserts.append(
            dict(
                id=stored.id,
                source=stored.source,
                symbol=symbol,
                inst_id=stored.inst_id,
                interval=stored.timeframe,
                open_at=stored.open_at,
                close_boundary=stored.close_boundary,
                o=stored.open,
                h=stored.high,
                l=stored.low,
                c=stored.close,
                volume_contracts=stored.volume_contracts,
                volume_base=stored.volume_base,
                turnover_quote=stored.turnover_quote,
                available_at=stored.available_at,
                revision_no=stored.revision_no,
                payload_hash=stored.market_content_hash,
            )
        )
    if inserts:
        await connection.execute(insert(s.market_bars).values(inserts))
    return tuple(result)


async def persist_observation(connection: AsyncConnection, observation: Observation) -> Observation:
    o = observation
    await content_lock(connection, f"observation:{o.source}:{o.inst_id}:{o.kind}:{o.event_at}")
    query = (
        sa.select(s.market_observations)
        .where(
            s.market_observations.c.source == o.source,
            s.market_observations.c.inst_id == o.inst_id,
            s.market_observations.c.kind == o.kind,
            s.market_observations.c.event_at == o.event_at,
        )
        .order_by(s.market_observations.c.revision_no.desc())
        .limit(1)
    )
    previous = (await connection.execute(query)).mappings().first()
    digest = content_hash(o.model_dump(exclude={"id", "available_at", "revision_no"}))
    if previous and previous["payload_hash"] == digest:
        return Observation.model_validate({key: previous[key] for key in Observation.model_fields})
    revision = previous["revision_no"] + 1 if previous else 1
    o = o.model_copy(update={"revision_no": revision, "id": uuid4() if previous else o.id})
    await connection.execute(
        insert(s.market_observations).values(
            **o.model_dump(exclude={"payload"}),
            payload=json_value(o.payload),
            payload_hash=digest,
        )
    )
    return o


async def persist_observations(connection: AsyncConnection, observations):
    """Batch external history with the same identity locks and append-only revisions."""
    observations = tuple(observations)
    if not observations:
        return ()
    if len(observations) > 1000:
        raise ValueError("外部观测批次超过1000条")
    keys = [(o.source, o.inst_id, o.kind, o.event_at) for o in observations]
    locks = (
        sa.values(sa.column("ordinal", sa.Integer), sa.column("key", sa.BigInteger))
        .data(
            [
                (i, content_lock_key(f"observation:{src}:{inst}:{kind}:{at}"))
                for i, (src, inst, kind, at) in enumerate(sorted(set(keys)))
            ]
        )
        .alias("observation_locks")
    )
    await connection.execute(
        sa.select(sa.func.pg_advisory_xact_lock(locks.c.key)).order_by(locks.c.ordinal)
    )
    columns = (
        s.market_observations.c.source,
        s.market_observations.c.inst_id,
        s.market_observations.c.kind,
        s.market_observations.c.event_at,
    )
    rows = (
        (
            await connection.execute(
                sa.select(s.market_observations)
                .where(sa.tuple_(*columns).in_(set(keys)))
                .distinct(*columns)
                .order_by(*columns, s.market_observations.c.revision_no.desc())
            )
        )
        .mappings()
        .all()
    )
    previous = {
        (r["source"], r["inst_id"], r["kind"], r["event_at"]): (
            Observation.model_validate({k: r[k] for k in Observation.model_fields}),
            r["payload_hash"],
        )
        for r in rows
    }
    result, inserts = [], []
    for observation, key in zip(observations, keys, strict=True):
        digest = content_hash(observation.model_dump(exclude={"id", "available_at", "revision_no"}))
        old = previous.get(key)
        if old and old[1] == digest:
            result.append(old[0])
            continue
        stored = observation.model_copy(
            update={"revision_no": old[0].revision_no + 1, "id": uuid4()} if old else {}
        )
        result.append(stored)
        inserts.append(
            {
                **stored.model_dump(exclude={"payload"}),
                "payload": json_value(stored.payload),
                "payload_hash": digest,
            }
        )
        previous[key] = (stored, digest)
    if inserts:
        await connection.execute(insert(s.market_observations).values(inserts))
    return tuple(result)


async def persist_snapshot(connection: AsyncConnection, snapshot: Snapshot) -> UUID:
    inserted = await connection.scalar(
        insert(s.snapshots)
        .values(
            id=snapshot.id,
            anchor_at=snapshot.anchor_at,
            decision_at=snapshot.decision_at,
            bar_ids_by_timeframe={
                g.timeframe: [str(b.id) for b in g.bars] for g in snapshot.groups
            },
            model_view_bar_ids={
                g.timeframe: [str(i) for i in g.model_bar_ids] for g in snapshot.groups
            },
            features_by_timeframe={g.timeframe: json_value(g.features) for g in snapshot.groups},
            external_observation_ids={
                g.timeframe: json_value(g.observation_ids) for g in snapshot.groups
            },
            feature_spec_version=snapshot.feature_spec_version,
            last_close_by_timeframe={
                g.timeframe: json_value(g.bars[-1].close_boundary) for g in snapshot.groups
            },
            payload=json_value(snapshot),
            payload_hash=snapshot.payload_hash,
        )
        .on_conflict_do_nothing(index_elements=[s.snapshots.c.payload_hash])
        .returning(s.snapshots.c.id)
    )
    if inserted is not None:
        await connection.execute(
            insert(s.snapshot_bars),
            [
                {"snapshot_id": inserted, "timeframe": g.timeframe, "position": n, "bar_id": b.id}
                for g in snapshot.groups
                for n, b in enumerate(g.bars)
            ],
        )
        return inserted
    return await connection.scalar(
        sa.select(s.snapshots.c.id).where(s.snapshots.c.payload_hash == snapshot.payload_hash)
    )


async def get_snapshot(connection: AsyncConnection, snapshot_id: UUID) -> Snapshot:
    payload = await connection.scalar(
        sa.select(s.snapshots.c.payload).where(s.snapshots.c.id == snapshot_id)
    )
    if payload is None:
        raise KeyError(snapshot_id)
    return Snapshot.model_validate(payload)
