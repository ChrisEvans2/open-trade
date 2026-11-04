import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import bars_at
from trading_quant import QuantConfig
from trading_quant import compute_features as calculate

from trading_agent.config import AppConfig, FeatureConfig
from trading_agent.domain.serialization import canonical_json
from trading_agent.market.features import compute_features

BASELINE = json.loads((Path(__file__).parents[1] / "fixtures/quant-v1-before.json").read_text())


@pytest.mark.parametrize("timeframe", ["1h", "4h", "6h", "12h"])
@pytest.mark.parametrize("flat", [False, True])
def test_complete_v1_outputs_match_before_move(timeframe, flat):
    anchor = datetime(2026, 10, 7, 8, tzinfo=UTC)
    values = compute_features(bars_at(anchor, timeframe, flat=flat), FeatureConfig())
    digest = hashlib.sha256(canonical_json(values).encode()).hexdigest()
    assert digest == BASELINE[timeframe + (":flat" if flat else ":trend")]


def test_external_v4_changes_engine_identity_but_preserves_shared_entrypoint():
    assert AppConfig().engine_config_hash != BASELINE["engine_config_hash"]
    anchor = datetime(2026, 10, 7, 8, tzinfo=UTC)
    raw = calculate(bars_at(anchor), QuantConfig())
    domain = compute_features(bars_at(anchor), FeatureConfig())
    assert [(item.name, item.value, item.quality) for item in raw] == [
        (item.name, item.value, item.quality) for item in domain
    ]
