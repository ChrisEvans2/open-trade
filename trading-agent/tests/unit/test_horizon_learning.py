from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from test_structure_predictions import START, condition, minute, prediction

from trading_agent.config import PredictionConfig
from trading_agent.domain.serialization import content_hash
from trading_agent.prediction.horizons import (
    paired_metrics,
    replay_pair,
    should_promote,
    should_rollback,
)


def samples(pair, n):
    return [pair.model_copy(update={"prediction_id": uuid4()}) for _ in range(n)]


def test_horizon_changes_only_deadline_never_historical_prediction():
    p = prediction()
    before = content_hash(p)
    bars = tuple(minute(i, close=104 if i >= 300 else 102) for i in range(360))
    pair = replay_pair(p, 4, 6, bars, START + timedelta(hours=6), PredictionConfig())
    assert (
        pair.valid and pair.incumbent.outcome == "EXPIRED" and pair.candidate.outcome == "ACHIEVED"
    )
    assert pair.incumbent.elapsed_bars == 4
    assert pair.candidate.elapsed_bars == Decimal(301) / 60
    metrics = paired_metrics(samples(pair, 40))
    assert (
        metrics["candidate"]["achieved_rate"] == 1 and metrics["incumbent"]["unresolved_rate"] == 1
    )
    assert should_promote(metrics, 40, PredictionConfig())
    assert not should_promote(paired_metrics(samples(pair, 39)), 40, PredictionConfig())
    assert content_hash(p) == before and p.report.horizon_bars == 2


def test_longer_deadline_waiting_cost_and_rollback():
    p = prediction()
    bars = tuple(minute(i) for i in range(360))
    config = PredictionConfig()
    unresolved = replay_pair(p, 4, 6, bars, START + timedelta(hours=6), config)
    metrics = paired_metrics(samples(unresolved, 40))
    assert metrics["gain"] == Decimal("-0.02")
    assert metrics["candidate"]["unresolved_rate"] == 1
    assert not should_promote(metrics, 40, config)
    invalidated = tuple(minute(i, close=94 if i >= 300 else 102) for i in range(360))
    worse = replay_pair(p, 4, 6, invalidated, START + timedelta(hours=6), config)
    metrics = paired_metrics(samples(worse, 20))
    assert should_rollback(metrics, config) and metrics["failure_increase"] == 1
    assert not should_rollback(paired_metrics(samples(worse, 19)), config)


def test_gap_ambiguity_and_untriggered_outcomes_remain_visible():
    config = PredictionConfig()
    p = prediction(invalidation=condition(95, "TOUCH"))
    bars = tuple(minute(i, high=105, low=94) if i == 1 else minute(i) for i in range(360))
    ambiguous = replay_pair(p, 4, 6, bars, START + timedelta(hours=6), config)
    assert not ambiguous.valid
    gaps = replay_pair(prediction(), 4, 6, bars[1:], START + timedelta(hours=6), config)
    assert gaps.data_quality == "AWAITING_DATA" and not gaps.valid
    no_trigger = tuple(minute(i, close=100) for i in range(360))
    pair = replay_pair(prediction(), 4, 6, no_trigger, START + timedelta(hours=6), config)
    metrics = paired_metrics([pair, gaps, ambiguous])
    assert metrics["n"] == 1 and metrics["unscored"] == 2
    assert metrics["candidate"]["untriggered_rate"] == 1


def test_failure_guard_rejects_apparent_utility_improvement():
    config = PredictionConfig()
    metrics = {"n": 40, "gain": Decimal("0.3"), "failure_increase": Decimal("0.021")}
    assert not should_promote(metrics, 40, config)
    metrics["failure_increase"] = Decimal("0.02")
    assert should_promote(metrics, 40, config)
