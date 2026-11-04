from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from trading_core.quantitative import CalculationError, QuantConfig, compute_features


def bars():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    result = []
    for index in range(80):
        close = Decimal(100 + index)
        boundary = start + timedelta(hours=index + 1)
        result.append(
            SimpleNamespace(
                inst_id="BTC-USDT-SWAP",
                timeframe="1h",
                close_boundary=boundary,
                available_at=boundary,
                high=close + 2,
                low=close - 2,
                close=close,
                volume_base=Decimal(index + 1),
                turnover_quote=close * Decimal(index + 1),
            )
        )
    return tuple(result)


def test_old_project_quantitative_module_uses_v1_math():
    features = {feature.name: feature for feature in compute_features(bars(), QuantConfig())}
    assert features["return_20"].value == pytest.approx(179 / 159 - 1)
    assert features["atr_pct"].value == pytest.approx(4 / 179)
    assert features["rsi"].value == features["adx"].value == 100
    assert features["rvol"].value == pytest.approx(80 / 69.5)
    assert features["cvd"].quality == "MISSING"


def test_v1_time_and_history_gates_are_preserved():
    with pytest.raises(CalculationError) as error:
        compute_features(bars()[:-1], QuantConfig())
    assert error.value.code == "INSUFFICIENT_HISTORY"
    with pytest.raises(CalculationError) as error:
        compute_features(bars(), QuantConfig(), decision_at=bars()[-2].available_at)
    assert error.value.code == "FUTURE_DATA"
