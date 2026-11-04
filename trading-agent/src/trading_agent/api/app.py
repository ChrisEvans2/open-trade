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
from trading_agent.api.contracts import ModelUsage
from trading_agent.api.controller import router as controller_router
from trading_agent.api.idempotency import idempotent
from trading_agent.api.prediction import ACTIONS as PREDICTION_ACTIONS
from trading_agent.api.prediction import router as prediction_router
from trading_agent.api.queries import Queries
from trading_agent.config import AppConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.prediction.contracts import ParentDecision, PredictionReport
from trading_agent.prediction.submissions import DecisionContent, PredictionContent
from trading_agent.storage import schema as s

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1_048_576


@dataclass
class APIServices:
    config: AppConfig
    credentials: CredentialHashes
    jobs: object
    engine_id: UUID
    readiness: object
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
    app.include_router(controller_router)

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
            if (
                request.url.path.startswith("/v1/research/")
                and request.url.path != "/v1/research/usage"
            ):
                raise DomainError(
                    "LEGACY_FLOW_RETIRED", "旧流程仅保留历史查询，请使用 v2 结构预测", 410
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
        if (
            request.url.path
            in {
                "/v2/research/prediction",
                "/v2/research/decision",
                "/v2/research/analysis/prediction",
                "/v2/research/analysis/decision",
            }
            and (attempt := getattr(request.state, "research_attempt", None)) is not None
            and (code := getattr(request.state, "research_error_code", None)) is not None
        ):
            try:
                await request.app.state.services.jobs.record_submission_rejection(
                    attempt, code, request.state.request_id
                )
                if attempt.payload.get("analysis_round") and not attempt.payload.get(
                    "analysis_managed"
                ):
                    async with request.app.state.services.jobs.transaction(attempt) as c:
                        await c.execute(
                            sa.update(s.analysis_scopes)
                            .where(
                                s.analysis_scopes.c.engine_id == UUID(attempt.payload["engine_id"]),
                                s.analysis_scopes.c.symbol == attempt.payload["symbol"],
                            )
                            .values(
                                parent_available=False,
                                error_code=code,
                                updated_at=await request.app.state.services.database.now(c),
                            )
                        )
            except (DomainError, sa.exc.SQLAlchemyError):
                # Expired/revoked attempts cannot append diagnostics either.
                logger.warning("submission_diagnostic_deferred")
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

    def error_response(request, code, message, status, *, issues=None):
        request.state.research_error_code = code
        error = {"code": code, "message": message, "request_id": request.state.request_id}
        if issues:
            error["issues"] = issues
        return JSONResponse(
            {"error": error},
            status_code=status,
        )

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not return submitted bodies (which may contain secrets) in error diagnostics.
        fields = set()
        for model in (PredictionReport, ParentDecision, PredictionContent, DecisionContent):
            schema = model.model_json_schema()
            for item in (schema, *schema.get("$defs", {}).values()):
                fields.update(item.get("properties", {}))
        issues = []
        for error in exc.errors()[:8]:
            path = [
                part
                for part in error["loc"]
                if (type(part) is int and 0 <= part <= 100)
                or (isinstance(part, str) and part in fields)
            ]
            kind = error["type"]
            if kind not in {
                "missing",
                "extra_forbidden",
                "value_error",
                "literal_error",
                "uuid_parsing",
                "decimal_parsing",
                "greater_than",
                "less_than",
                "greater_than_equal",
                "less_than_equal",
                "list_type",
                "dict_type",
                "model_type",
                "string_type",
                "int_type",
                "bool_type",
                "finite_number",
                "too_long",
                "too_short",
                "string_too_long",
                "string_too_short",
                "decimal_max_digits",
                "decimal_max_places",
            }:
                kind = "type_error"
            issues.append({"path": path, "type": kind})
        return error_response(
            request, "REQUEST_INVALID", "请求字段、格式或契约不合格", 422, issues=issues
        )

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

    @app.post("/v1/research/usage")
    async def submit_usage(payload: ModelUsage, request: Request):
        attempt = await require_attempt(request, "usage")
        await request.app.state.services.jobs.record_model_usage(attempt, payload.runs)
        return {"recorded": True}

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

    async def set_pause(request, paused, *, preserve_risk_halts=False):
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
                if not paused and not preserve_risk_halts:
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
                {
                    "paused": paused,
                    **({"preserve_risk_halts": True} if preserve_risk_halts else {}),
                },
                operation,
            )
        return JSONResponse(body, status_code=code)

    @app.post("/v1/schedule/pause")
    async def pause(request: Request):
        return await set_pause(request, True)

    @app.post("/v1/schedule/resume")
    async def resume(request: Request, preserve_risk_halts: bool = False):
        return await set_pause(request, False, preserve_risk_halts=preserve_risk_halts)

    return app
