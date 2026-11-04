from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert

from trading_agent.api.auth import CredentialHashes, require_attempt, require_identity
from legacy_research.api.contracts import JobRequest, ModelUsage, TimeframesRequest
from trading_agent.api.idempotency import idempotent
from trading_agent.api.prediction import ACTIONS as PREDICTION_ACTIONS
from trading_agent.api.prediction import router as prediction_router
from trading_agent.api.queries import Queries
from trading_agent.config import AppConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from legacy_research.forecasts.contracts import ForecastSubmission
from legacy_research.reviews.contracts import ReviewSubmission
from trading_agent.storage import schema as s

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1_048_576


@dataclass
class APIServices:
    config: AppConfig
    credentials: CredentialHashes
    jobs: object
    forecasts: object
    engine_id: UUID
    readiness: object
    reviews: object | None = None
    predictions: object | None = None
    knowledge: object | None = None

    @property
    def database(self):
        return self.jobs.database


def create_app(services: APIServices | None = None, *, runtime_factory=None):
    @asynccontextmanager
    async def lifespan(app):
        if runtime_factory:
            runtime = runtime_factory()
            await runtime.start()
            app.state.services = runtime.api_services()
            try:
                yield
            finally:
                await runtime.stop()
        else:
            yield

    app = FastAPI(
        title="Trading research runtime",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if services:
        app.state.services = services
    app.include_router(prediction_router)

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        try:
            if request.url.path not in {"/healthz", "/v1/healthz"}:
                actions = {
                    **PREDICTION_ACTIONS,
                    "/v1/research/context": "context",
                    "/v1/research/timeframes": "timeframes",
                    "/v1/research/forecast": "forecast",
                    "/v1/research/review": "review",
                    "/v1/research/usage": "usage",
                }
                if action := actions.get(request.url.path):
                    await require_attempt(request, action)
                else:
                    operator = request.url.path.startswith(("/v1/fills/", "/v1/schedule/")) or (
                        request.method == "POST" and request.url.path.startswith("/v1/strategies/")
                    )
                    await require_identity(
                        request, ("operator",) if operator else ("controller", "operator")
                    )
            # Authenticate before consuming or validating the body; bound chunked requests too.
            chunks, size = [], 0
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_REQUEST_BYTES:
                        raise DomainError("REQUEST_TOO_LARGE", "请求超过1MiB限额", 413)
                    chunks.append(chunk)
            request._body = b"".join(
                chunks
            )  # Starlette's cached request replays this body downstream.
            response = await call_next(request)
        except DomainError as exc:
            response = error_response(request, exc.code, exc.message, exc.status)
        except sa.exc.SQLAlchemyError:
            response = error_response(request, "DATABASE_UNAVAILABLE", "数据库操作暂不可用", 503)
        except TimeoutError:
            response = error_response(request, "REQUEST_TIMEOUT", "请求接收超时", 408)
        except Exception:
            logger.error("api_request_failed", extra={"request_id": request.state.request_id})
            response = error_response(request, "INTERNAL_ERROR", "请求暂时无法完成", 500)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        logger.info(
            "api_request_finished",
            extra={
                "request_id": request.state.request_id,
                "route": request.scope.get("route").path
                if request.scope.get("route")
                else "unknown",
                "status_code": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            },
        )
        return response

    def error_response(request, code, message, status):
        return JSONResponse(
            {"error": {"code": code, "message": message, "request_id": request.state.request_id}},
            status_code=status,
        )

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not return submitted bodies (which may contain secrets) in error diagnostics.
        return error_response(request, "REQUEST_INVALID", "请求字段、格式或契约不合格", 422)

    @app.exception_handler(sa.exc.SQLAlchemyError)
    async def database_error(request, exc):
        return error_response(request, "DATABASE_UNAVAILABLE", "数据库操作暂不可用", 503)

    def queries(request):
        return Queries(request.app.state.services.database)

    @app.get("/healthz")
    @app.get("/v1/healthz")
    async def health():
        return {"alive": True}

    @app.get("/readyz")
    @app.get("/v1/readyz")
    async def ready(request: Request):
        await require_identity(request)
        result = await request.app.state.services.readiness()
        return JSONResponse(json_value(result), status_code=200 if result["ready"] else 503)

    @app.get("/v1/status")
    async def status(request: Request):
        await require_identity(request)
        return await queries(request).status()

    @app.get("/v1/forecasts/latest")
    async def latest(request: Request):
        await require_identity(request)
        return await queries(request).forecast()

    @app.get("/v1/forecasts/{ident}")
    async def forecast(ident: UUID, request: Request):
        await require_identity(request)
        return await queries(request).forecast(ident)

    @app.get("/v1/jobs/{ident}")
    async def job(ident: UUID, request: Request):
        await require_identity(request)
        return await queries(request).job(ident)

    @app.post("/v1/jobs")
    async def create_job(payload: JobRequest, request: Request):
        identity = await require_identity(request)
        services = request.app.state.services
        async with services.database.transaction() as connection:
            await services.jobs.ownership.guard(connection)

            async def operation(connection):
                now = await services.database.now(connection)
                if payload.kind == "explain":
                    if not await connection.scalar(
                        sa.select(s.forecasts.c.id).where(s.forecasts.c.id == payload.forecast_id)
                    ):
                        raise DomainError("FORECAST_NOT_FOUND", "预测不存在", 404)
                elif payload.kind == "review" and payload.range_end > now:
                    raise DomainError("FUTURE_REVIEW", "复盘区间不能包括未来数据")
                elif payload.kind == "experiment" and not services.config.experiment.enabled:
                    raise DomainError("EXPERIMENT_DISABLED", "合约模拟规则未确定，收益实验禁用")
                kind = {"explain": "EXPLAIN", "review": "REVIEW_DAILY", "experiment": "EXPERIMENT"}[
                    payload.kind
                ]
                record = payload.model_dump(exclude_none=True, exclude={"kind"})
                record["engine_id"] = str(services.engine_id)
                record["scope"] = "REQUESTED"
                ident = await services.jobs.create(
                    connection,
                    kind,
                    f"request:{identity.actor_id}:{request.headers.get('Idempotency-Key')}",
                    record,
                    now,
                )
                return 202, {"job_id": ident}

            code, body = await idempotent(
                connection,
                identity.actor_id,
                "/v1/jobs",
                request.headers.get("Idempotency-Key"),
                payload,
                operation,
            )
        return JSONResponse(body, status_code=code)

    @app.get("/v1/research/context")
    async def research_context(request: Request):
        attempt = await require_attempt(request, "context")
        if request.query_params:
            raise DomainError(
                "CONTEXT_SCOPE_FORBIDDEN", "上下文由任务令牌定位，不接受查询参数", 403
            )
        services = request.app.state.services
        if session := request.headers.get("X-Hermes-Session"):
            await services.jobs.record_session(attempt, session)
        if attempt.kind == "FORECAST":
            return await services.forecasts.context(attempt)
        return await services.reviews.context(attempt)

    @app.post("/v1/research/usage")
    async def submit_usage(payload: ModelUsage, request: Request):
        attempt = await require_attempt(request, "usage")
        await request.app.state.services.jobs.record_model_usage(attempt, payload.runs)
        return {"recorded": True}

    @app.post("/v1/research/review")
    async def submit_review(payload: ReviewSubmission, request: Request):
        attempt = await require_attempt(request, "review")
        return await request.app.state.services.reviews.submit(attempt, payload)

    @app.post("/v1/research/timeframes")
    async def timeframes(payload: TimeframesRequest, request: Request):
        attempt = await require_attempt(request, "timeframes")
        service = request.app.state.services.forecasts
        result = {"trends": {}}
        for child in payload.trends:
            result["trends"][child.report.timeframe] = str(
                await service.submit_trend(attempt, child.report, child.child_id)
            )
        if payload.freeze:
            result["bundle_id"] = str(await service.freeze_bundle(attempt))
        if payload.one_hour:
            result["one_hour_analysis_id"] = str(
                await service.submit_one_hour(
                    attempt, payload.one_hour.report, payload.one_hour.child_id
                )
            )
        return result

    @app.post("/v1/research/forecast")
    async def publish(payload: ForecastSubmission, request: Request):
        attempt = await require_attempt(request, "forecast")
        return {
            "forecast_id": str(await request.app.state.services.forecasts.publish(attempt, payload))
        }

    @app.get("/v1/strategies")
    async def strategies(
        request: Request, cursor: int | None = None, limit: int = Query(default=20, ge=1, le=100)
    ):
        await require_identity(request)
        return await queries(request).strategies(cursor, limit)

    @app.post("/v1/fills/import")
    @app.post("/v1/strategies/{ident}/approve")
    @app.post("/v1/strategies/{ident}/reject")
    @app.post("/v1/strategies/rollback")
    async def contract_gate(request: Request):
        await require_identity(request, ("operator",))
        raise DomainError(
            "CONTRACT_SPEC_PENDING", "合约规格尚未确定，成交导入与策略审批保持关闭", 409
        )

    @app.get("/v1/experiments/{ident}")
    async def experiment(ident: UUID, request: Request):
        await require_identity(request)
        return await queries(request).object(s.experiments, ident, "EXPERIMENT_NOT_FOUND")

    @app.get("/v1/reviews/{ident}")
    async def review(ident: UUID, request: Request):
        await require_identity(request)
        return await queries(request).object(s.reviews, ident, "REVIEW_NOT_FOUND")

    async def set_pause(request, paused):
        identity = await require_identity(request, ("operator",))
        services = request.app.state.services
        route = "/v1/schedule/pause" if paused else "/v1/schedule/resume"
        async with services.database.transaction() as connection:
            await services.jobs.ownership.guard(connection, exclusive=True)

            async def operation(connection):
                await connection.execute(
                    sa.update(s.runtime_control)
                    .where(s.runtime_control.c.id == 1)
                    .values(paused=paused)
                )
                if not paused:
                    await connection.execute(
                        sa.update(s.trading_accounts).values(
                            body=s.trading_accounts.c.body.op("-")("risk_halt"),
                            revision=s.trading_accounts.c.revision + 1,
                        )
                    )
                await connection.execute(
                    insert(s.audit_events).values(
                        actor_type="operator",
                        actor_id=identity.actor_id,
                        action="SCHEDULE_PAUSED" if paused else "SCHEDULE_RESUMED",
                        object_type="schedule",
                        request_id=request.state.request_id,
                    )
                )
                return 200, {"paused": paused}

            code, body = await idempotent(
                connection,
                identity.actor_id,
                route,
                request.headers.get("Idempotency-Key"),
                {"paused": paused},
                operation,
            )
        return JSONResponse(body, status_code=code)

    @app.post("/v1/schedule/pause")
    async def pause(request: Request):
        return await set_pause(request, True)

    @app.post("/v1/schedule/resume")
    async def resume(request: Request):
        return await set_pause(request, False)

    return app
