from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock


async def idempotent(connection, actor_id, route, key, payload, operation):
    if not key or len(key) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "需要1至128位可见ASCII幂等键")
    digest = content_hash(payload)
    await content_lock(connection, f"request:{actor_id}:{route}:{key}")
    previous = (
        (
            await connection.execute(
                sa.select(s.idempotency_requests).where(
                    s.idempotency_requests.c.actor_id == actor_id,
                    s.idempotency_requests.c.route == route,
                    s.idempotency_requests.c.key == key,
                )
            )
        )
        .mappings()
        .first()
    )
    if previous:
        if previous["request_hash"] != digest:
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同幂等键对应不同请求", 409)
        return previous["response_status"], previous["response_body"]
    status, result = await operation(connection)
    result = json_value(result)
    await connection.execute(
        insert(s.idempotency_requests).values(
            actor_id=actor_id,
            route=route,
            key=key,
            request_hash=digest,
            response_status=status,
            response_body=result,
        )
    )
    return status, result
