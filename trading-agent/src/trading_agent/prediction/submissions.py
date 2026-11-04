"""Model-facing content contracts; all database identities are bound by the service."""

from typing import Annotated, Literal

from pydantic import Field

from trading_agent.config import StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.prediction.contracts import (
    Direction,
    DirectionProbabilities,
    ParentDecision,
    PredictionReport,
    Scenario,
)

Reference = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[a-z0-9_.]+$")]


class PredictionContent(DirectionProbabilities):
    structure_ref: Reference
    horizon_ref: Reference
    primary_direction: Direction | None = None
    scenarios: tuple[Scenario, ...] = Field(default=(), max_length=2)
    reason: str = Field(min_length=1, max_length=2000)
    risk_notes: tuple[Annotated[str, Field(max_length=300)], ...] = Field(default=(), max_length=5)
    evidence_refs: tuple[Reference, ...] = Field(default=(), max_length=30)
    knowledge_refs: tuple[Reference, ...] = Field(default=(), max_length=10)


class DecisionContent(StrictModel):
    prediction: PredictionContent
    action: Literal["OPEN_LONG", "OPEN_SHORT", "HOLD", "CLOSE"]
    trade_style: Literal["TREND", "RANGE", "BREAKOUT"]
    selected_direction: Direction | None = None
    child_refs: tuple[Reference, ...] = Field(default=(), max_length=4)
    relationship: str = Field(min_length=1, max_length=2000)


def reference_maps(context):
    structures = {"structure.unknown": None}
    structures.update({f"structure.{i}": c for i, c in enumerate(context["structures"], 1)})
    horizons = {f"horizon.{i}": r for i, r in enumerate(context["horizon_rules"], 1)}
    evidence = {
        "synthesis.current"
        if context.get("input_kind") == "CHILD_SYNTHESIS"
        else "market.current": context["input_id"]
    }
    evidence.update(
        {ref: source["input_id"] for ref, source in context.get("child_sources", {}).items()}
    )
    evidence.update({f"bar.{i}": value for i, value in enumerate(context["bar_ids"], 1)})
    evidence.update(
        {f"observation.{i}": v for i, v in enumerate(context.get("observation_ids", []), 1)}
    )
    knowledge = {}
    for recall in context.get("knowledge_recalls", []):
        for card in recall["items"]:
            if card["id"] not in knowledge.values():
                knowledge[f"knowledge.{len(knowledge) + 1}"] = card["id"]
    children = {f"child.{c['report']['timeframe']}": c["id"] for c in context.get("children", [])}
    return structures, horizons, evidence, knowledge, children


def resolve(mapping, ref, code="REFERENCE_SCOPE"):
    if ref not in mapping:
        raise DomainError(code, "引用不属于本次授权输入或引用类型错误", 422)
    return mapping[ref]


def bind_prediction(context, content: PredictionContent):
    structures, horizons, evidence, knowledge, _ = reference_maps(context)
    candidate = resolve(structures, content.structure_ref)
    rule = resolve(horizons, content.horizon_ref)
    return PredictionReport(
        symbol=context["symbol"],
        timeframe=context["timeframe"],
        structure_id=candidate["id"] if candidate else None,
        structure=candidate["kind"] if candidate else "UNKNOWN",
        stage=candidate["stage"] if candidate else "UNKNOWN",
        horizon_rule_id=rule["id"],
        horizon_bars=rule["bars"],
        **content.model_dump(
            exclude={"structure_ref", "horizon_ref", "evidence_refs", "knowledge_refs"}
        ),
        evidence_ids=[resolve(evidence, r) for r in content.evidence_refs],
        knowledge_version_ids=[resolve(knowledge, r) for r in content.knowledge_refs],
    )


def bind_decision(context, content: DecisionContent):
    children = reference_maps(context)[4]
    if len(set(content.child_refs)) != len(content.child_refs):
        raise DomainError("REFERENCE_SCOPE", "引用不能重复", 422)
    for ref in content.child_refs:
        resolve(children, ref)
    position = context.get("position")
    return ParentDecision(
        prediction=bind_prediction(context, content.prediction),
        **content.model_dump(exclude={"prediction", "child_refs"}),
        child_prediction_ids=list(children.values()),
        context_revision=context["context_revision"],
        position_id=position["position_id"] if position else None,
    )


def model_context(context):
    structures, horizons, evidence, knowledge, children = reference_maps(context)
    cards = {
        card["id"]: card
        for recall in context.get("knowledge_recalls", [])
        for card in recall["items"]
    }
    forbidden = {"id", "structure_id", "horizon_rule_id", "evidence_ids", "knowledge_version_ids"}
    value = {
        "model_contract": 4,
        **{
            k: context[k]
            for k in (
                "symbol",
                "timeframe",
                "role",
                "decision_at",
                "reference_price",
                "features",
                "indicator_policy",
                "funding_context",
                "external_data_diagnostics",
                "btc_benchmark",
                "fallback_lines",
                "fallback_tolerance",
            )
            if k in context
        },
        "model_bars": [
            {k: v for k, v in bar.items() if k != "id"} for bar in context.get("model_bars", [])
        ],
        "forming_candle": context.get("forming_candle"),
        "forming_candle_quality": context.get(
            "forming_candle_quality",
            {
                "quality": "MISSING",
                "missing_reason": "LEGACY_INPUT",
            },
        ),
        "provisional_features": context.get("provisional_features", []),
        "provisional_feature_basis": context.get("provisional_feature_basis"),
        "structures": [
            {
                "ref": ref,
                **(
                    {k: v for k, v in c.items() if k != "id"}
                    if c
                    else {"kind": "UNKNOWN", "stage": "UNKNOWN"}
                ),
            }
            for ref, c in structures.items()
        ],
        "horizon_rules": [
            {"ref": ref, **{k: v for k, v in r.items() if k != "id"}} for ref, r in horizons.items()
        ],
        "evidence_options": list(evidence),
        "knowledge": [
            {"ref": ref, **{k: v for k, v in cards[ident].items() if k != "id"}}
            for ref, ident in knowledge.items()
        ],
        "children": [
            {
                "ref": f"child.{c['report']['timeframe']}",
                "expires_at": c["expires_at"],
                "published_at": c["published_at"],
                "data_times": context.get("child_data_times", {}).get(
                    f"child.{c['report']['timeframe']}"
                ),
                "report": {k: v for k, v in c["report"].items() if k not in forbidden},
            }
            for c in context.get("children", [])
        ],
        "child_options": list(children),
        "position": None
        if not context.get("position")
        else {k: v for k, v in context["position"].items() if k in {"status", "symbol"}}
        | {
            k: v
            for k, v in context["position"].get("body", {}).items()
            if k in {"direction", "contracts", "entry_price", "stop_price", "take_profit_price"}
        },
    }

    if context.get("input_kind") == "CHILD_SYNTHESIS":
        for key in (
            "reference_price",
            "features",
            "model_bars",
            "forming_candle",
            "forming_candle_quality",
            "provisional_features",
            "provisional_feature_basis",
            "fallback_lines",
            "fallback_tolerance",
        ):
            value.pop(key, None)
        value.update(
            input_kind="CHILD_SYNTHESIS",
            boundary_options=context["boundary_options"],
            missing_timeframes=context["missing_timeframes"],
            evidence_read="research_context(child_ref=child.Nh)",
        )
    from trading_agent.market.context import BTC_RELATIVE, indicator_policy

    value.setdefault("indicator_policy", indicator_policy(()))
    if "btc_benchmark" in value:
        value["btc_benchmark"] = {
            k: v
            for k, v in value["btc_benchmark"].items()
            if k not in {"calculation_bars", "bar_ids"}
        } | {"calculation_bars_count": len(context["btc_benchmark"].get("calculation_bars", []))}
    if "features" in value:
        value["features"] = [
            {**f, "evidence_strength": "WEAK"} if f["name"] in BTC_RELATIVE else f
            for f in value["features"]
        ]
    return value
