from datetime import timedelta
from decimal import Decimal

import pytest

from trading_agent.domain.errors import DomainError
from trading_agent.market.observations import parse_observations


def test_published_funding_keeps_future_settlement_out_of_event_time(anchor):
    at = anchor + timedelta(seconds=65)
    row = {
        "instId": "fixture-contract",
        "ts": str(int(at.timestamp() * 1000)),
        "fundingTime": str(int((anchor + timedelta(hours=8)).timestamp() * 1000)),
        "fundingRate": "0.0001",
    }
    item = parse_observations("funding-rate", "fixture-contract", [row], at)[0]
    assert item.event_at == at and item.available_at == at
    assert item.payload["published_rate"] == Decimal("0.0001")
    assert "realized_rate" not in item.payload
    row["instId"] = "another-contract"
    with pytest.raises(DomainError) as exc:
        parse_observations("funding-rate", "fixture-contract", [row], at)
    assert exc.value.code == "OBSERVATION_INSTRUMENT_MISMATCH"


def test_history_is_realized_and_never_backdates_availability(anchor):
    row = {
        "instId": "fixture-contract",
        "fundingTime": str(int(anchor.timestamp() * 1000)),
        "fundingRate": "0.0002",
        "realizedRate": "0.0001",
    }
    acquired = anchor + timedelta(days=1)
    item = parse_observations("funding-rate-history", "fixture-contract", [row], acquired)[0]
    assert item.event_at == anchor and item.available_at == acquired
    assert item.payload["realized_rate"] == Decimal("0.0001")
    assert "published_rate" not in item.payload
    row["realizedRate"] = "NaN"
    with pytest.raises(ValueError):
        parse_observations("funding-rate-history", "fixture-contract", [row], acquired)
