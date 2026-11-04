import httpx
import pytest
from test_analysis_rounds import finish, publish, start
from test_analysis_rounds import round_system as round_system
from test_forming_candle_inputs import CurrentMarket
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
async def test_children_receive_30_plus_one_and_parent_only_child_synthesis(round_system, symbol):
    system = round_system
    source = CurrentMarket(
        (system.db, system.clock, system.jobs, system.predictions, system.engine)
    )
    system.rounds.market.client = source
    try:
        main = await start(system, symbol)
        seen = []
        for role in ("1h", "4h", "6h", "12h", "parent"):
            stage = await system.rounds.begin(main, role)
            view = stage["context"]
            assert view["model_contract"] == 4
            assert view["symbol"] == symbol
            if role == "parent":
                assert view["input_kind"] == "CHILD_SYNTHESIS"
                assert (
                    not {
                        "model_bars",
                        "features",
                        "forming_candle",
                        "reference_price",
                        "provisional_features",
                    }
                    & view.keys()
                )
                assert len(view["children"]) == 4
                assert all(c["data_times"]["provisional_count"] == 26 for c in view["children"])
                assert len(source.candle_calls) == (8 if symbol == "ETHUSDT" else 4)
                app = create_app(
                    APIServices(
                        system.predictions.config,
                        CredentialHashes("a" * 64, "b" * 64),
                        system.jobs,
                        system.engine,
                        None,
                        predictions=system.predictions,
                        knowledge=system.knowledge,
                    )
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://fixture"
                ) as client:
                    response = await client.get(
                        "/v2/research/context?child_ref=child.1h",
                        headers={
                            "Authorization": "Bearer " + main.token,
                            "X-Analysis-Run": stage["run"],
                        },
                    )
                assert response.status_code == 200, response.text
                evidence = response.json()["context"]
                assert len(evidence["model_bars"]) == 30
                assert evidence["forming_candle"]["timeframe"] == "1h"
                assert len(evidence["provisional_features"]) == 26
                assert len(source.candle_calls) == (8 if symbol == "ETHUSDT" else 4)
                await publish(system, main, stage)
                continue
            assert len(view["model_bars"]) == 30
            current = view["forming_candle"]
            assert current["timeframe"] == ("1h" if role == "parent" else role)
            assert current["status"] == "PROVISIONAL" and current["confirm"] == "0"
            assert view["model_bars"][-1]["close_boundary"] == current["open_at"]
            assert all(b["close_boundary"] <= current["open_at"] for b in view["model_bars"])
            assert len(view["features"]) == 37 and len(view["provisional_features"]) == 26
            assert all(v["status"] == "PROVISIONAL" for v in view["provisional_features"])
            assert view["provisional_feature_basis"]["closed_bars"] == 80
            assert view["provisional_feature_basis"]["forming_bars"] == 1
            seen.append(current["timeframe"])
            await publish(system, main, stage)
        assert seen == ["1h", "4h", "6h", "12h"]
        await finish(system, main)
    finally:
        await source.http.close()
