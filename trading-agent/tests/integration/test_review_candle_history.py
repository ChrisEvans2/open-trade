import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from test_forming_candle_inputs import CurrentMarket
from test_prediction_knowledge import knowledge
from test_prediction_pipeline import minute, report, task
from test_prediction_pipeline import prediction_system as prediction_system
from test_review_market_windows import ReviewMarket

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import boundary
from trading_agent.jobs.service import token_hash
from trading_agent.prediction.market import PredictionMarket
from trading_agent.runtime.agent_runner import AgentRunner
from trading_agent.runtime.prompts import load_prompts
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar, row_bar

pytestmark = pytest.mark.postgres
TARGET = datetime(2026, 10, 30, 7, 23, 45, tzinfo=UTC)


class HistoricalMarket(ReviewMarket):
    def __init__(self, system, fault=None):
        super().__init__()
        self.system, self.fault, self.minute_calls = system, fault, []
        self.checkouts = system[0].engine.pool.checkedout()

    async def minute_window(self, inst_id, start, end, deadline, *, max_pages):
        assert self.system[0].engine.pool.checkedout() == self.checkouts
        assert max_pages == 3
        self.minute_calls.append((inst_id, start, end))
        if self.fault == "error":
            raise DomainError("MARKET_REJECTED", "fixture API failure", 503)
        rows = tuple(
            minute(inst_id, start + timedelta(minutes=i)).model_copy(
                update={"available_at": self.system[1].value}
            )
            for i in range(int((end - start).total_seconds() // 60))
        )
        if self.fault == "gap":
            return rows[:10] + rows[11:]
        if self.fault == "future":
            return rows + (minute(inst_id, end),)
        return rows


async def saved_input(db, ident):
    async with db.engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    sa.select(s.prediction_inputs).where(s.prediction_inputs.c.id == ident)
                )
            )
            .mappings()
            .one()
        )


async def publish_case(system, service, *, symbol="BTCUSDT", timeframe="4h", snapshot=False):
    attempt = await task(system, symbol=symbol, timeframe=timeframe, freeze=not snapshot)
    if snapshot:
        client = CurrentMarket(system)
        try:
            await PredictionMarket(system[2], client, system[3], service, system[4]).prepare(
                attempt
            )
        finally:
            await client.http.close()
    context = await system[3].context(attempt)
    original = await saved_input(system[0], UUID(context["input_id"]))
    ident = await system[3].publish(attempt, await report(system, attempt))
    await system[2].finish(attempt, result_id=ident)
    return ident, original


async def evaluate_case(system, ident, symbol="BTCUSDT"):
    verifier = await task(system, symbol=symbol, kind="PREDICT_VERIFY", freeze=False)
    await system[3].verify(
        verifier,
        ident,
        (
            minute(
                symbol.removesuffix("USDT") + "-USDT-SWAP",
                boundary(TARGET, "1m") + timedelta(minutes=1),
            ),
        ),
    )
    await system[2].finish(verifier)


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
async def test_prepare_freezes_original_or_reconstruction_and_exposes_scoped_model_details(
    prediction_system,
    tmp_path,
    symbol,
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    clock.value = TARGET
    service = knowledge(system, tmp_path)
    observed, original_observed = await publish_case(system, service, symbol=symbol, snapshot=True)
    reconstructed, original_missing = await publish_case(
        system, service, symbol=symbol, timeframe="6h"
    )
    clock.value += timedelta(minutes=2)
    for ident in (observed, reconstructed):
        await evaluate_case(system, ident, symbol)
    # A later revision of a CLOSED source bar must not enter the historical 80+1 calculation.
    async with db.transaction() as conn:
        row = (
            (
                await conn.execute(
                    sa.select(s.market_bars).where(
                        s.market_bars.c.id == UUID(original_missing["body"]["bar_ids"][-1])
                    )
                )
            )
            .mappings()
            .one()
        )
        revised = row_bar(row).model_copy(
            update={
                "id": uuid4(),
                "open": Decimal(900),
                "high": Decimal(902),
                "low": Decimal(898),
                "close": Decimal(901),
                "available_at": clock.value,
            }
        )
        await persist_bar(conn, revised, symbol)
    client = HistoricalMarket(system)
    collector = PredictionMarket(jobs, client, predictions, service, engine)
    reviewer = await task(system, symbol=symbol, kind="PREDICT_REVIEW", freeze=False)
    ident = await collector.prepare(reviewer)
    overview = await service.review_context(reviewer)
    assert "case_inputs" not in overview and len(overview["cases"]) == 2
    assert len(client.minute_calls) == 1  # Original snapshot needs no new historical fetch.
    assert client.minute_calls[0][2] == TARGET.replace(second=0)
    first = (await service.review_context(reviewer, prediction_id=observed))["case"]
    second = (await service.review_context(reviewer, prediction_id=reconstructed))["case"]
    assert first["original_input"]["body"] == original_observed["body"]
    assert first["candle_reconstruction"] is None
    assert len(first["original_input"]["body"]["model_bars"]) == 30
    assert first["original_input"]["body"]["forming_candle"]["status"] == "PROVISIONAL"
    assert second["original_input"]["body"] == original_missing["body"]
    rebuilt = second["candle_reconstruction"]
    assert rebuilt["quality"] == "PARTIAL_COVERAGE" and rebuilt["uncovered_seconds"] == 45
    assert rebuilt["forming_candle"]["close"] == "182"
    assert rebuilt["forming_candle"]["status"] == "RECONSTRUCTED"
    assert rebuilt["originally_observed"] is False
    assert len(rebuilt["provisional_features"]) == 26
    assert "minute_source" not in rebuilt
    assert next(
        f["value"] for f in rebuilt["provisional_features"] if f["name"] == "return_1"
    ) == pytest.approx(182 / 179 - 1)
    frozen = await saved_input(db, ident)
    assert frozen["content_hash"] == content_hash(frozen["body"])
    audit = frozen["body"]["case_inputs"][str(reconstructed)]["candle_reconstruction"]
    assert audit["minute_source_hash"] == content_hash(audit["minute_source"])
    # Exercise the actual runner input file, not only a helper's dictionary.
    runner = AgentRunner(jobs, predictions, SimpleNamespace(), load_prompts(), knowledge=service)
    runner.config = runner.config.model_copy(
        update={
            "runtime": runner.config.runtime.model_copy(update={"data_dir": tmp_path / "runner"})
        }
    )
    _, folder, _ = await runner._inputs(reviewer)
    written = json.loads((folder / "context.json").read_text())
    assert written == overview and "case_inputs" not in written
    # The same guarded API used by research_context allows only this review's frozen cases.
    services = APIServices(
        predictions.config,
        CredentialHashes(token_hash("controller"), token_hash("operator")),
        jobs,
        engine,
        None,
        predictions=predictions,
        knowledge=service,
    )
    headers = {"Authorization": "Bearer " + reviewer.token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as api:
        result = await api.get(
            "/v2/research/context", params={"prediction_id": str(reconstructed)}, headers=headers
        )
        assert result.status_code == 200 and result.json()["case"] == second
        assert (
            await api.get(
                "/v2/research/context", params={"prediction_id": str(uuid4())}, headers=headers
            )
        ).status_code == 403
        assert (
            await api.get(
                "/v2/research/context",
                params={"prediction_id": str(observed), "symbol": "OTHER"},
                headers=headers,
            )
        ).status_code == 403
    client.fault = "error"
    clock.value += timedelta(seconds=1)
    assert await collector.prepare(reviewer) == ident
    assert len(client.minute_calls) == 1 and await service.review_context(reviewer) == overview
    assert await saved_input(db, original_missing["id"]) == original_missing
    assert await saved_input(db, original_observed["id"]) == original_observed


@pytest.mark.parametrize(
    "fault,code",
    [
        ("gap", "MINUTE_COVERAGE_GAP"),
        ("error", "MARKET_REJECTED"),
        ("future", "RECONSTRUCTION_SCOPE"),
    ],
)
async def test_missing_minutes_never_fabricate_a_snapshot(prediction_system, tmp_path, fault, code):
    system = prediction_system
    system[1].value = TARGET
    service = knowledge(system, tmp_path)
    prediction, original = await publish_case(system, service)
    system[1].value += timedelta(minutes=2)
    await evaluate_case(system, prediction)
    client = HistoricalMarket(system, fault)
    reviewer = await task(system, kind="PREDICT_REVIEW", freeze=False)
    await PredictionMarket(system[2], client, system[3], service, system[4]).prepare(reviewer)
    case = (await service.review_context(reviewer, prediction_id=prediction))["case"]
    assert case["candle_reconstruction"]["quality"] == "MISSING"
    assert case["candle_reconstruction"]["missing_reason"] == code
    assert "forming_candle" not in case["candle_reconstruction"]
    assert case["original_input"]["body"] == original["body"]
    assert await saved_input(system[0], original["id"]) == original


async def test_expired_attempt_cannot_persist_new_review_evidence(prediction_system, tmp_path):
    system = prediction_system
    system[1].value = TARGET
    service = knowledge(system, tmp_path)
    ident, _ = await publish_case(system, service)
    system[1].value += timedelta(minutes=2)
    await evaluate_case(system, ident)

    class ExpiringMarket(HistoricalMarket):
        async def minute_window(self, *args, **kwargs):
            result = await super().minute_window(*args, **kwargs)
            system[1].value += timedelta(seconds=61)
            return result

    reviewer = await task(system, kind="PREDICT_REVIEW", freeze=False)
    collector = PredictionMarket(system[2], ExpiringMarket(system), system[3], service, system[4])
    with pytest.raises(DomainError):
        await collector.prepare(reviewer)
    async with system[0].engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.prediction_inputs)
                .where(s.prediction_inputs.c.job_id == reviewer.job_id)
            )
            == 0
        )
