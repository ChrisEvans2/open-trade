from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_pipeline import minute, publish_child, report, task
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import TIMEFRAMES, boundary
from trading_agent.knowledge.contracts import KnowledgeChange, KnowledgeReview
from trading_agent.knowledge.service import KnowledgeService
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


def knowledge(system, tmp_path):
    config = system[3].config.model_copy(
        update={
            "prediction": system[3].config.prediction.model_copy(
                update={"knowledge_index_path": tmp_path / "knowledge.sqlite3"}
            )
        }
    )
    return KnowledgeService(system[2], config)


async def review_task(system, service):
    attempt = await task(system, kind="PREDICT_REVIEW", freeze=False)
    anchor = boundary(system[1].value)
    await service.freeze_review(
        attempt, system[4], {tf: bars_at(anchor, tf) for tf in TIMEFRAMES}, anchor=anchor
    )
    return attempt


def change(**overrides):
    return KnowledgeChange(
        **{
            **dict(
                operation="NEW",
                symbol="BTCUSDT",
                title="回踩失败的反例检查",
                summary="先失效后达标仍然失效，需区分方向与边界。",
                applicability="BTC结构趋势预测",
                limitations="样本尚少，不可外推胜率",
                content="记录当时的结构边界、目标、期限及后续价格过程。",
                procedure=["核对当时证据", "查询失败案例", "分别评估方向和目标"],
                evidence_status="HYPOTHESIS",
                rationale="由预测复盘提出，等待更多验证",
            ),
            **overrides,
        }
    )


async def test_seed_unverified_search_freeze_index_recovery_and_pagination(
    prediction_system, tmp_path
):
    system = prediction_system
    service = knowledge(system, tmp_path)
    assert len(await service.seed()) == 4
    assert not await service.seed()
    attempt = await task(system)
    found = await service.search(attempt, "上升三角", structure="ASCENDING_TRIANGLE")
    assert found["degraded"] and len(found["items"]) == 1
    card = found["items"][0]
    assert card["status"] == "UNVERIFIED"
    first = await service.expand(attempt, UUID(card["id"]), limit=120)
    second = await service.expand(attempt, UUID(card["id"]), offset=120, limit=120)
    assert first["content_hash"] == second["content_hash"] and first["next_offset"] == 120
    with pytest.raises(DomainError, match="本任务已经检索"):
        await service.expand(attempt, uuid4())
    await service.rebuild()
    # Original recall is immutable, including degraded status; new query can use recovered index.
    assert await service.search(attempt, "上升三角", structure="ASCENDING_TRIANGLE") == found
    recovered = await service.search(attempt, "三角", structure="ASCENDING_TRIANGLE")
    assert not recovered["degraded"] and recovered["items"][0]["id"] == card["id"]
    system_report = await report(system, attempt)
    await system[3].publish(
        attempt, system_report.model_copy(update={"knowledge_version_ids": (UUID(card["id"]),)})
    )
    with pytest.raises(DomainError, match="只有预测复盘"):
        await service.submit(attempt, KnowledgeReview(narrative="越权", changes=[change()]))


async def test_pg_commit_survives_index_failure_and_no_future_version_leak(
    prediction_system, tmp_path, monkeypatch
):
    system = prediction_system
    service = knowledge(system, tmp_path)
    await service.seed()
    old_attempt = await task(system)
    old = await service.search(old_attempt, "W底", structure="W_BOTTOM")
    original = old["items"][0]
    # Advance within lease. This new version must not leak into the older frozen context.
    system[1].value += timedelta(seconds=1)
    reviewer = await review_task(system, service)
    review = KnowledgeReview(
        narrative="修订初始假设，保留未验证状态",
        changes=[
            change(
                operation="REVISE",
                concept_id=original["concept_id"],
                previous_version_ids=[original["id"]],
                structure="W_BOTTOM",
                title="W底新版",
                summary="新的检索词：颈线回踩等待确认。",
            )
        ],
    )
    ident = await service.submit(reviewer, review)
    assert ident == await service.submit(reviewer, review)
    historical = await service.search(old_attempt, "W底", structure="W_BOTTOM", limit=1)
    assert historical["items"][0]["id"] == original["id"]
    new_attempt = await task(system)
    new = await service.search(new_attempt, "颈线回踩", structure="W_BOTTOM")
    assert new["degraded"] and new["items"][0]["revision"] == 2
    index_attempt = await system[2].claim(("KNOWLEDGE_INDEX",))

    def failed(_rows):
        raise OSError("fixture disk full")

    monkeypatch.setattr(service.index, "upsert", failed)
    with pytest.raises(OSError):
        await service.sync(index_attempt)
    async with system[0].engine.connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.knowledge_versions)) == 5
    monkeypatch.undo()
    await service.sync(index_attempt)
    # Process the subsequent revision job, then verify complete PG rebuild parity.
    index_attempt = await system[2].claim(("KNOWLEDGE_INDEX",))
    await service.sync(index_attempt)
    indexed = await service.search(new_attempt, "颈线", structure="W_BOTTOM")
    assert not indexed["degraded"]
    assert await service.rebuild() == 5
    again = await service.search(new_attempt, "新版", structure="W_BOTTOM")
    assert again["items"][0]["id"] == new["items"][0]["id"]
    with pytest.raises(DomainError, match="已经更新"):
        await service.submit(await review_task(system, service), review)


async def test_merge_and_retire_are_append_only(prediction_system, tmp_path):
    system = prediction_system
    service = knowledge(system, tmp_path)
    await service.seed()
    attempt = await review_task(system, service)
    cards = (await service.search(attempt))["items"]
    selected = cards[:2]
    merged = change(
        operation="MERGE",
        symbol=None,
        previous_version_ids=[c["id"] for c in selected],
        title="结构证据核验",
        summary="合并两份操作指引，仍需实际案例支持。",
    )
    await service.submit(attempt, KnowledgeReview(narrative="合并重复检查步骤", changes=[merged]))
    # Scope before latest-version selection used to incorrectly resurrect retired records.
    system[1].value += timedelta(seconds=1)
    current = await task(system)
    cards = (await service.search(current))["items"]
    assert len(cards) == 3
    assert not {c["concept_id"] for c in selected} & {c["concept_id"] for c in cards}
    async with system[0].engine.connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.knowledge_versions)) == 7


async def test_review_cases_calibration_and_fabricated_success_rejected(
    prediction_system, tmp_path
):
    system = prediction_system
    service = knowledge(system, tmp_path)
    ident = await publish_child(system)
    start = system[1].value.replace(minute=2, second=0)
    system[1].value += timedelta(hours=4)
    verifier = await task(system, kind="PREDICT_VERIFY", freeze=False)
    bars = tuple(
        minute(
            "BTC-USDT-SWAP",
            start + timedelta(minutes=i),
            high="205" if i else "185",
            low="181",
            close="184",
        )
        for i in range(239)
    )
    result = await system[3].verify(verifier, ident, bars)
    assert result.state.observation_complete and result.state.lifecycle == "ACHIEVED"
    reviewer = await review_task(system, service)
    context = await service.review_context(reviewer)
    assert context["metrics"]["outcomes"] == {"ACHIEVED": 1}
    assert context["metrics"]["direction_bins"]["UP:0.7"]["mean_brier"] == "0.09"
    case = context["cases"][0]
    link = dict(
        prediction_id=case["prediction_id"], evaluation_id=case["evaluation_id"], relation="SUPPORT"
    )
    await service.submit(
        reviewer,
        KnowledgeReview(
            narrative="成功案例支持待验证知识",
            changes=[change(evidence_status="EVIDENCE_SUPPORTED", cases=[link])],
        ),
    )
    current = await task(system)
    card = (await service.search(current, "回踩失败"))["items"][0]
    content = await service.expand(current, UUID(card["id"]), limit=12000)
    assert str(ident) in content["content"] and '"cases":[' in content["content"]
    forged = change(cases=[{**link, "prediction_id": str(uuid4())}])
    with pytest.raises(DomainError, match="已验证事实"):
        await service.submit(
            await review_task(system, service),
            KnowledgeReview(narrative="伪造案例", changes=[forged]),
        )


async def test_review_selects_latest_visible_revision_within_symbol_and_half_open_range(
    prediction_system, tmp_path
):
    system = prediction_system
    service = knowledge(system, tmp_path)
    btc = await publish_child(system)
    eth = await publish_child(system, symbol="ETHUSDT")
    system[1].value += timedelta(minutes=2)
    for symbol, ident in (("BTCUSDT", btc), ("ETHUSDT", eth)):
        verifier = await task(system, symbol=symbol, kind="PREDICT_VERIFY", freeze=False)
        opened = boundary(system[1].value, "1m") - timedelta(minutes=1)
        await system[3].verify(
            verifier, ident, (minute(symbol.removesuffix("USDT") + "-USDT-SWAP", opened),)
        )
    async with system[0].transaction() as conn:
        visible = (
            (
                await conn.execute(
                    sa.select(s.prediction_evaluations).where(
                        s.prediction_evaluations.c.prediction_id == btc
                    )
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            s.prediction_evaluations.insert().values(
                prediction_id=btc,
                attempt_id=visible["attempt_id"],
                revision=visible["revision"] + 1,
                body=visible["body"],
                evaluated_at=system[1].value + timedelta(minutes=1),
            )
        )
    end = system[1].value
    # This BTC prediction is exactly at the excluded upper boundary.
    await publish_child(system, timeframe="4h")
    reviewer = await task(system, kind="PREDICT_REVIEW", freeze=False)
    reviewer = replace(
        reviewer,
        payload={
            **reviewer.payload,
            "range_start": (end - timedelta(hours=1)).isoformat(),
            "range_end": end.isoformat(),
        },
    )
    anchor = boundary(end)
    await service.freeze_review(
        reviewer, system[4], {tf: bars_at(anchor, tf) for tf in TIMEFRAMES}, anchor=anchor
    )
    cases = (await service.review_context(reviewer))["cases"]
    assert len(cases) == 1
    assert cases[0]["prediction_id"] == str(btc)
    assert cases[0]["evaluation_id"] == str(visible["id"])


async def test_structure_stage_guidance_is_recalled_separately(prediction_system, tmp_path):
    system = prediction_system
    service = knowledge(system, tmp_path)
    reviewer = await review_task(system, service)
    await service.submit(
        reviewer,
        KnowledgeReview(
            narrative="区分形成、突破、回踩和失效的检查步骤",
            changes=[
                change(
                    structure="W_BOTTOM",
                    stage=stage,
                    title=f"W底{stage}",
                    summary=f"W底{stage}阶段的边界检查",
                )
                for stage in ("FORMING", "BREAKOUT_UP", "RETEST_UP", "INVALIDATED")
            ],
        ),
    )
    await service.rebuild()
    attempt = await task(system)
    for stage in ("FORMING", "BREAKOUT_UP", "RETEST_UP", "INVALIDATED"):
        cards = await service.search(attempt, "W底", structure="W_BOTTOM", stage=stage)
        assert len(cards["items"]) == 1 and cards["items"][0]["stage"] == stage
