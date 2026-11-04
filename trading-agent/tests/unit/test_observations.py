from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import bars_at
from pydantic import ValidationError

from trading_agent.config import AppConfig, FeatureConfig, HermesConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.market.features import compute_features
from trading_agent.market.models import Observation
from trading_agent.market.snapshots import Snapshot, build_snapshot


def test_current_funding_is_decision_time_context_at_every_timeframe(anchor):
    o = Observation(
        source="fixture",
        inst_id="BTC-USDT-SWAP",
        kind="funding",
        event_at=anchor + timedelta(seconds=30),
        available_at=anchor + timedelta(seconds=60),
        unit="ratio",
        payload={"published_rate": "0.0001", "event_type": "published"},
    )
    snapshot = build_snapshot(
        anchor,
        anchor + timedelta(seconds=65),
        {tf: bars_at(anchor, tf) for tf in ("1h", "4h", "6h", "12h")},
        FeatureConfig(),
        (o,),
    )
    by_name = {f.name: f for f in snapshot.group("1h").features}
    assert by_name["funding_rate"].value == Decimal("0.0001")
    assert snapshot.group("1h").observation_ids == (o.id,)
    for tf in ("4h", "6h", "12h"):
        group = snapshot.group(tf)
        assert group.observation_ids == (o.id,)
        current = next(f for f in group.features if f.name == "funding_rate")
        assert current.value == Decimal("0.0001")
        assert current.event_at == o.event_at  # Never relabel current publication as past close.
    assert Snapshot.model_validate(json_value(snapshot)).payload_hash == snapshot.payload_hash


def test_complete_taker_window_and_gap_quality(anchor):
    observations = tuple(
        Observation(
            source="fixture",
            inst_id="BTC-USDT-SWAP",
            kind="taker_volume",
            unit="base_quantity",
            event_at=anchor - timedelta(hours=19 - i),
            available_at=anchor + timedelta(seconds=60),
            coverage_start=anchor - timedelta(hours=20 - i),
            coverage_end=anchor - timedelta(hours=19 - i),
            payload={"buy": "2", "sell": "1"},
        )
        for i in range(20)
    )
    data = {f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), observations)}
    assert data["cvd"].value == 20
    assert data["taker_buy_sell_ratio"].value == 2
    partial = {
        f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), observations[1:])
    }
    assert partial["cvd"].quality == "PARTIAL_COVERAGE"
    assert partial["cvd"].value is None


def test_no_future_observation_and_required_feature_gate(anchor):
    future = Observation(
        source="fixture",
        inst_id="BTC-USDT-SWAP",
        kind="funding",
        unit="ratio",
        event_at=anchor,
        available_at=anchor + timedelta(seconds=66),
        payload={"published_rate": "0.0001"},
    )
    data = {
        f.name: f
        for f in compute_features(
            bars_at(anchor), FeatureConfig(), (future,), anchor + timedelta(seconds=65)
        )
    }
    assert data["funding_rate"].quality == "MISSING"
    with pytest.raises(DomainError, match="必需"):
        compute_features(
            bars_at(anchor),
            FeatureConfig(required_features=("cvd",), optional_features=("return_1",)),
        )


def test_engine_hash_survives_server_path_changes():
    local = AppConfig(
        hermes=HermesConfig(
            executable=Path("/local/hermes"),
            provider="fixture",
            model="fixture",
            deployment_lock=Path("/local/lock"),
        )
    )
    remote = local.model_copy(
        update={
            "hermes": local.hermes.model_copy(
                update={
                    "executable": Path("/opt/hermes/bin/hermes"),
                    "deployment_lock": Path("/etc/ta/lock"),
                }
            )
        }
    )
    assert local.engine_config_hash == remote.engine_config_hash
    changed = local.model_copy(
        update={"hermes": local.hermes.model_copy(update={"model": "other"})}
    )
    assert local.engine_config_hash != changed.engine_config_hash


def test_source_precision_cannot_silently_round_in_postgres(anchor):
    bar = bars_at(anchor)[-1]
    with pytest.raises(ValidationError):
        type(bar).model_validate({**bar.model_dump(), "volume_base": "1.0000000000001"})
