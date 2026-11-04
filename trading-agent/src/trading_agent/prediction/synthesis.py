"""Parent evidence comes exclusively from the selected, immutable child inputs."""

from uuid import UUID

import sqlalchemy as sa

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.storage import schema as s


async def source_input(connection, child, engine_id, symbol, expected_hash=None):
    row = (
        (
            await connection.execute(
                sa.select(s.prediction_inputs).where(
                    s.prediction_inputs.c.id == UUID(child["snapshot_id"]),
                    s.prediction_inputs.c.engine_id == engine_id,
                    s.prediction_inputs.c.symbol == symbol,
                    s.prediction_inputs.c.timeframe == child["report"]["timeframe"],
                    s.prediction_inputs.c.role == "TIMEFRAME",
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or content_hash(row["body"]) != row["content_hash"]
        or (expected_hash is not None and expected_hash != row["content_hash"])
    ):
        raise DomainError("CHILD_INPUT_INTEGRITY", "子预测冻结证据校验失败", 409)
    return row


async def synthesis_inputs(connection, children, engine_id, symbol):
    if not children:
        raise DomainError("MISSING_CONTEXT", "没有有效子预测，不能生成父综合输入", 409)
    sources, structures, boundaries, summaries = {}, {}, [], {}
    risk_basis = None
    for child in children:
        row = await source_input(connection, child, engine_id, symbol)
        body = row["body"]
        tf = child["report"]["timeframe"]
        ref = f"child.{tf}"
        sources[ref] = {"input_id": str(row["id"]), "content_hash": row["content_hash"]}
        forming = body.get("forming_candle")
        summaries[ref] = {
            "decision_at": body["decision_at"],
            "last_closed_at": body["model_bars"][-1]["close_boundary"],
            "forming_status": "PROVISIONAL" if forming else "MISSING",
            "forming_as_of": forming["available_at"] if forming else None,
            "provisional_count": len(body.get("provisional_features", [])),
            "forming_missing_reason": body.get("forming_candle_quality", {}).get("missing_reason"),
            "volume_time_normalized": False,
        }
        selected = child["report"]["structure_id"]
        for candidate in body["structures"]:
            if candidate["id"] == selected:
                structures[candidate["id"]] = {**candidate, "source_child_ref": ref}
        # Retain the exact geometry AND tolerance that the child report used.
        for scenario in child["report"]["scenarios"]:
            for condition in [
                scenario["trigger"],
                scenario["invalidation"],
                *scenario.get("boundary_expectations", []),
            ]:
                item = {"source_child_ref": ref, **condition}
                if item not in boundaries:
                    boundaries.append(item)
        if tf == "1h":
            atr = next(
                (f for f in body["features"] if f["name"] == "atr_pct" and f["quality"] == "OK"),
                None,
            )
            if atr:
                risk_basis = {
                    "source_child_ref": ref,
                    "input_id": str(row["id"]),
                    "decision_at": body["decision_at"],
                    "atr_pct": atr["value"],
                }
    # Preserve the published-fact price field; not a new quote or a model input.
    anchor = max(children, key=lambda child: (child["published_at"], child["report"]["timeframe"]))
    return {
        "input_kind": "CHILD_SYNTHESIS",
        "child_sources": sources,
        "child_data_times": summaries,
        "structures": list(structures.values()),
        "boundary_options": boundaries,
        "fallback_lines": [],
        "fallback_tolerance": "0",
        "bar_ids": [],
        "observation_ids": [],
        "reference_price": anchor["reference_price"],
        "reference_price_source": f"child.{anchor['report']['timeframe']}",
        "execution_basis": risk_basis,
    }
