"""Controller Hermes requests work from the single runtime; it never executes orders."""

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert

from trading_agent.api.auth import require_identity
from trading_agent.api.contracts import JobRequest
from trading_agent.api.idempotency import idempotent
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.storage import schema as s

router = APIRouter()


@router.post("/v1/jobs")
@router.post("/v2/jobs")
async def create_job(payload: JobRequest, request: Request):
    identity = await require_identity(request)
    services = request.app.state.services
    if services.predictions is None or services.knowledge is None:
        raise DomainError("PREDICTION_NOT_READY", "结构预测服务尚未初始化", 503)
    async with services.database.transaction() as connection:
        await services.jobs.ownership.guard(connection)

        async def operation(connection):
            now = await services.database.now(connection)
            if payload.kind == "experiment":
                if not services.config.experiment.enabled:
                    raise DomainError("EXPERIMENT_DISABLED", "收益实验仍受配置门禁保护", 409)
                raise DomainError("CONTRACT_IMPLEMENTATION_NOT_READY", "合约模拟规格尚未验收", 409)
            if await connection.scalar(
                sa.select(s.runtime_control.c.paused).where(s.runtime_control.c.id == 1)
            ):
                raise DomainError("SCHEDULE_PAUSED", "分析调度暂停中，仍可查询已有事实", 409)
            record = payload.model_dump(exclude_none=True, exclude={"kind", "symbol"})
            if payload.kind == "explain":
                ident = payload.prediction_id or payload.forecast_id
                fact = (
                    (
                        await connection.execute(
                            sa.select(s.prediction_facts).where(s.prediction_facts.c.id == ident)
                        )
                    )
                    .mappings()
                    .first()
                )
                target_field = "prediction_id"
                if fact is None and payload.forecast_id:
                    fact = (
                        (
                            await connection.execute(
                                sa.select(s.forecasts).where(s.forecasts.c.id == ident)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    target_field = "forecast_id"
                if fact is None:
                    raise DomainError("PREDICTION_NOT_FOUND", "指定预测不存在", 404)
                if fact["published_at"] > now:
                    raise DomainError("FUTURE_DATA", "指定预测尚未发布", 409)
                symbols = (fact["symbol"],)
                record = {target_field: str(ident)}
            else:
                if payload.range_end > now:
                    raise DomainError("FUTURE_REVIEW", "复盘区间不能包括未来数据", 422)
                symbols = (
                    (payload.symbol,) if payload.symbol else services.config.prediction.symbols
                )
            if any(symbol not in services.config.prediction.symbols for symbol in symbols):
                raise DomainError("SYMBOL_SCOPE", "请求币种不在当前部署范围内", 422)
            identifiers = []
            for symbol in symbols:
                body = {
                    **record,
                    "engine_id": str(services.engine_id),
                    "symbol": symbol,
                    "request_kind": payload.kind,
                    "requested_by": identity.actor_id,
                }
                ident = await services.jobs.create(
                    connection,
                    "PREDICT_REVIEW",
                    f"controller:{identity.actor_id}:{request.headers.get('Idempotency-Key')}:{symbol}",
                    body,
                    now,
                )
                identifiers.append(ident)
                await connection.execute(
                    insert(s.audit_events).values(
                        actor_type=identity.role,
                        actor_id=identity.actor_id,
                        action="controller_job_requested",
                        object_type="job",
                        object_id=ident,
                        after_hash=content_hash(body),
                        request_id=request.state.request_id,
                        created_at=now,
                    )
                )
            return 202, {"job_id": identifiers[0], "job_ids": identifiers}

        # Both URLs represent the same controller command, including retry deduplication.
        code, body = await idempotent(
            connection,
            identity.actor_id,
            "/v2/jobs",
            request.headers.get("Idempotency-Key"),
            payload,
            operation,
        )
    return JSONResponse(body, status_code=code)


@router.get("/v2/positions")
async def positions(request: Request):
    await require_identity(request)
    services = request.app.state.services
    async with services.database.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(s.trading_positions)
                    .where(s.trading_positions.c.engine_id == services.engine_id)
                    .order_by(s.trading_positions.c.symbol)
                )
            )
            .mappings()
            .all()
        )
    return json_value({"items": [dict(row) for row in rows]})
