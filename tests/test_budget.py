# tests/test_budget.py
import pytest

from app.agent.budget import BudgetTracker


def test_cost_of_uses_provider_pricing():
    tracker = BudgetTracker(cap_usd=2.00)
    cost = tracker.cost_of(tokens_in=1000, tokens_out=1000, provider_name="bedrock-primary")
    assert cost > 0


def test_cost_of_unknown_provider_raises():
    tracker = BudgetTracker(cap_usd=2.00)
    with pytest.raises(KeyError):
        tracker.cost_of(1000, 1000, "unknown-provider")


def test_would_exceed_false_when_under_cap():
    tracker = BudgetTracker(cap_usd=2.00, cost_used_usd=1.00)
    assert tracker.would_exceed(0.50) is False


def test_would_exceed_true_when_over_cap():
    tracker = BudgetTracker(cap_usd=2.00, cost_used_usd=1.80)
    assert tracker.would_exceed(0.50) is True


def test_record_accumulates_cost():
    tracker = BudgetTracker(cap_usd=2.00)
    tracker.record(0.30)
    tracker.record(0.20)
    assert tracker.cost_used_usd == pytest.approx(0.50)
