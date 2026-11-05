"""Atomic page/cursor persistence under the runtime ownership fence."""

import json
import zlib
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.domain.errors import StaleAttempt
from trading_agent.domain.serialization import json_value
from trading_agent.storage import schema as s


def scope(table, inst, start):
    return (table.c.inst_id == inst) & (table.c.open_at == start)


async def load_checkpoint(database, inst, start):
    async with database.transaction() as conn:
        # One SQL snapshot for cursor AND pages; no external I/O in this transaction.
        c, p = s.trade_minute_checkpoints, s.trade_minute_pages
        records = (
            await conn.execute(
                sa.select(c.c.state, p.c.body_zlib)
                .select_from(c.join(p, (c.c.inst_id == p.c.inst_id) & (c.c.open_at == p.c.open_at)))
                .where(scope(c, inst, start))
                .order_by(p.c.page_no)
            )
        ).all()
    if not records:
        return None
    state = dict(records[0].state)
    state["rows"] = [
        row for record in records for row in json.loads(zlib.decompress(record.body_zlib))
    ]
    return state


async def save_page(database, ownership, inst, start, state, rows):
    body = zlib.compress(json.dumps(json_value(rows), separators=(",", ":")).encode())
    c, p = s.trade_minute_checkpoints, s.trade_minute_pages
    async with database.transaction() as conn:
        await ownership.guard(conn)
        now = await database.now(conn)
        if start >= now or datetime.fromisoformat(state["candle"]["close_boundary"]) > now:
            raise StaleAttempt()
        previous = await conn.scalar(
            sa.select(c.c.state).where(scope(c, inst, start)).with_for_update()
        )
        if (previous["pages"] if previous else 0) != state["pages"] - 1:
            raise StaleAttempt()
        await conn.execute(
            insert(c)
            .values(inst_id=inst, open_at=start, state=json_value(state), updated_at=now)
            .on_conflict_do_update(
                index_elements=[c.c.inst_id, c.c.open_at],
                set_={"state": json_value(state), "updated_at": now},
            )
        )
        await conn.execute(
            insert(p).values(inst_id=inst, open_at=start, page_no=state["pages"], body_zlib=body)
        )


async def clear_checkpoint(conn, inst, start):
    await conn.execute(
        sa.delete(s.trade_minute_checkpoints).where(scope(s.trade_minute_checkpoints, inst, start))
    )
