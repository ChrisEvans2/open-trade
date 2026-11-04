"""Versioned research contract. There is intentionally no model execution endpoint."""

from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from pydantic import Field

from trading_agent.api.auth import require_attempt, require_identity
from trading_agent.api.contracts import ModelUsage
from trading_agent.config import StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.knowledge.contracts import KnowledgeReview
from trading_agent.prediction.contracts import (
    ParentDecision,
    PredictionReport,
    Stage,
    StructureKind,
    Symbol,
    Timeframe,
)
from trading_agent.prediction.submissions import DecisionContent, PredictionContent, model_context
from trading_agent.storage import schema as s

router = APIRouter(prefix="/v2")
ACTIONS = {
    "/v2/research/context": "prediction_context",
    "/v2/research/prediction": "prediction_submit",
    "/v2/research/decision": "decision_submit",
    "/v2/research/knowledge/search": "knowledge",
    "/v2/research/knowledge/content": "knowledge",
    "/v2/research/review": "knowledge_review",
    "/v2/research/usage": "usage",
    "/v2/research/analysis": "analysis",
    "/v2/research/analysis/start": "analysis",
    "/v2/research/analysis/fail": "analysis",
    "/v2/research/analysis/complete": "analysis",
    "/v2/research/analysis/knowledge": "knowledge",
    "/v2/research/analysis/prediction": "analysis",
    "/v2/research/analysis/decision": "analysis",
}


class KnowledgeQuery(StrictModel):
    query: str = Field(default="", max_length=300)
    structure: StructureKind | None = None
    stage: Stage | None = None
    limit: int = Field(default=5, ge=1, le=5)


class RoleStart(StrictModel):
    role: str = Field(pattern=r"^(1h|4h|6h|12h|parent)$")


class RoleFailure(StrictModel):
    run: UUID
    code: str = Field(pattern=r"^[A-Z][A-Z_0-9]{0,63}$")


class RoleComplete(StrictModel):
    run: UUID


def analysis(request):
    value = getattr(services(request).predictions, "rounds", None)
    if value is None:
        raise DomainError("ANALYSIS_NOT_READY", "轮次服务未初始化", 503)
    return value


@router.get("/research/analysis")
async def analysis_state(request: Request):
    attempt = await require_attempt(request, "analysis")
    return await analysis(request).state(attempt)


@router.post("/research/analysis/start")
async def analysis_start(payload: RoleStart, request: Request):
    attempt = await require_attempt(request, "analysis")
    return await analysis(request).begin(attempt, payload.role)


@router.post("/research/analysis/fail")
async def analysis_fail(payload: RoleFailure, request: Request):
    attempt = await require_attempt(request, "analysis")
    return await analysis(request).reject(attempt, payload.run, payload.code)


@router.post("/research/analysis/complete")
async def analysis_complete(payload: RoleComplete, request: Request):
    attempt = await require_attempt(request, "analysis")
    await analysis(request).complete_execution(attempt, payload.run)
    return {"completed": True}


@router.get("/research/analysis/knowledge")
async def analysis_knowledge(request: Request, ref: str, offset: int = 0, limit: int = 6000):
    from trading_agent.prediction.submissions import reference_maps, resolve

    attempt = await require_attempt(request, "knowledge")
    if not attempt.payload.get("analysis_managed"):
        raise DomainError("ANALYSIS_SCOPE", "知识引用必须绑定当前角色", 403)
    context = await services(request).predictions.context(attempt)
    ident = resolve(reference_maps(context)[3], ref)
    result = await services(request).knowledge.expand(
        attempt, UUID(ident), offset=offset, limit=limit
    )
    return {k: v for k, v in result.items() if k not in {"version_id", "id"}}


@router.post("/research/analysis/prediction")
async def analysis_prediction(payload: PredictionContent, request: Request):
    attempt = await require_attempt(request, "analysis")
    run = request.headers.get("X-Analysis-Run")
    if not run:
        raise DomainError("ANALYSIS_SCOPE", "缺少工具绑定的角色", 403)
    return await analysis(request).submit(attempt, run, payload)


@router.post("/research/analysis/decision")
async def analysis_decision(payload: DecisionContent, request: Request):
    attempt = await require_attempt(request, "analysis")
    run = request.headers.get("X-Analysis-Run")
    if not run:
        raise DomainError("ANALYSIS_SCOPE", "缺少工具绑定的角色", 403)
    return await analysis(request).submit(attempt, run, payload)


def services(request):
    value = request.app.state.services
    if value.predictions is None or value.knowledge is None:
        raise DomainError("PREDICTION_NOT_READY", "结构预测服务尚未初始化", 503)
    return value


@router.get("/research/context")
async def context(request: Request):
    attempt = await require_attempt(request, "prediction_context")
    if request.query_params and not (
        (
            (
                attempt.kind == "PREDICT_REVIEW"
                and list(request.query_params.keys()) == ["prediction_id"]
            )
            or (
                attempt.kind == "PREDICT_PARENT"
                and list(request.query_params.keys()) == ["child_ref"]
            )
        )
        and len(request.query_params.multi_items()) == 1
    ):
        raise DomainError("CONTEXT_SCOPE_FORBIDDEN", "上下文由任务令牌定位，不接受范围参数", 403)
    value = services(request)
    if session := request.headers.get("X-Hermes-Session"):
        await value.jobs.record_session(attempt, session)
    if "child_ref" in request.query_params:
        return await value.predictions.child_evidence(attempt, request.query_params["child_ref"])
    if attempt.kind == "PREDICT_REVIEW":
        return await value.knowledge.review_context(
            attempt, prediction_id=request.query_params.get("prediction_id")
        )
    if attempt.payload.get("analysis_round") and not attempt.payload.get("analysis_managed"):
        return await analysis(request).state(attempt)
    context = await value.predictions.context(attempt)
    return model_context(context) if attempt.payload.get("analysis_managed") else context


@router.post("/research/prediction")
async def prediction(payload: PredictionReport, request: Request):
    attempt = await require_attempt(request, "prediction_submit")
    return {"prediction_id": str(await services(request).predictions.publish(attempt, payload))}


@router.post("/research/decision")
async def decision(payload: ParentDecision, request: Request):
    attempt = await require_attempt(request, "decision_submit")
    return {"prediction_id": str(await services(request).predictions.publish(attempt, payload))}


@router.post("/research/knowledge/search")
async def knowledge_search(payload: KnowledgeQuery, request: Request):
    attempt = await require_attempt(request, "knowledge")
    value = services(request)
    result = await value.knowledge.search(attempt, **payload.model_dump())
    if attempt.payload.get("analysis_managed"):
        context = await value.predictions.context(attempt)
        return {"items": model_context(context)["knowledge"]}
    return result


@router.get("/research/knowledge/content")
async def knowledge_content(
    request: Request,
    version_id: UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=6000, ge=1, le=12000),
):
    attempt = await require_attempt(request, "knowledge")
    allowed = {"version_id", "offset", "limit"}
    if set(request.query_params) - allowed:
        raise DomainError("KNOWLEDGE_SCOPE", "知识展开不接受额外范围参数", 403)
    return await services(request).knowledge.expand(attempt, version_id, offset=offset, limit=limit)


@router.post("/research/review")
async def review(payload: KnowledgeReview, request: Request):
    attempt = await require_attempt(request, "knowledge_review")
    return {"review_id": str(await services(request).knowledge.submit(attempt, payload))}


@router.post("/research/usage")
async def usage(payload: ModelUsage, request: Request):
    attempt = await require_attempt(request, "usage")
    await services(request).jobs.record_model_usage(attempt, payload.runs)
    return {"recorded": True}


@router.get("/predictions/latest")
async def latest(
    request: Request, symbol: Symbol, timeframe: Timeframe = "1h", parent: bool = True
):
    await require_identity(request)
    value = services(request)
    async with value.database.engine.connect() as connection:
        if parent and (rounds := getattr(value.predictions, "rounds", None)):
            if not await rounds.parent_available(connection, value.engine_id, symbol):
                raise DomainError(
                    "PREDICTION_UNAVAILABLE",
                    "error或更新中：当前无可用父结论，旧预测仅供历史查询",
                    503,
                )
        row = (
            (
                await connection.execute(
                    sa.select(
                        s.prediction_facts.c.body,
                        s.prediction_states.c.body.label("verification"),
                        s.prediction_states.c.data_quality,
                    )
                    .join(
                        s.prediction_heads,
                        s.prediction_heads.c.prediction_id == s.prediction_facts.c.id,
                    )
                    .join(
                        s.prediction_states,
                        s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                    )
                    .where(
                        s.prediction_heads.c.engine_id == value.engine_id,
                        s.prediction_heads.c.symbol == symbol,
                        s.prediction_heads.c.timeframe == timeframe,
                        s.prediction_heads.c.role == ("PARENT" if parent else "TIMEFRAME"),
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("PREDICTION_NOT_FOUND", "该范围还没有预测", 404)
        return json_value(dict(row))


@router.get("/predictions/{ident}")
async def historical(ident: UUID, request: Request):
    await require_identity(request)
    async with services(request).database.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(s.prediction_facts).where(s.prediction_facts.c.id == ident)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("PREDICTION_NOT_FOUND", "预测不存在", 404)
        return json_value(dict(row))


@router.get("/intents/{ident}")
async def intent(ident: UUID, request: Request):
    await require_identity(request)
    async with services(request).database.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(
                        s.trade_intents,
                        s.trade_intent_states.c.status,
                        s.trade_intent_states.c.body.label("execution_state"),
                    )
                    .join(
                        s.trade_intent_states,
                        s.trade_intent_states.c.intent_id == s.trade_intents.c.id,
                    )
                    .where(s.trade_intents.c.id == ident)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("INTENT_NOT_FOUND", "交易意图不存在", 404)
        return json_value(dict(row))
