import pytest

from governance.budget import BudgetExceeded, BudgetLedger
from governance.policy import BudgetPolicy, Policy


def test_token_budget_is_enforced():
    ledger = BudgetLedger(BudgetPolicy(max_tokens=100))
    ledger.charge(tokens=60, label="step-1")
    assert ledger.remaining()["tokens"] == 40
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.charge(tokens=60, label="step-2")
    assert excinfo.value.reason == "token"
    assert excinfo.value.used["tokens"] == 120


def test_cost_budget_is_enforced():
    ledger = BudgetLedger(BudgetPolicy(max_cost_usd=1.0))
    ledger.charge(cost=0.6)
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.charge(cost=0.6)
    assert excinfo.value.reason == "cost_usd"


def test_negative_charge_is_rejected():
    ledger = BudgetLedger(BudgetPolicy())
    with pytest.raises(ValueError):
        ledger.charge(tokens=-1)


def test_parallel_slots_and_agent_cap():
    ledger = BudgetLedger(BudgetPolicy(max_agents=4, max_parallel_agents=2))
    ledger.acquire()
    ledger.acquire()
    assert ledger.active_slots == 2
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.acquire()
    assert excinfo.value.reason == "parallel_agents"

    ledger.release()
    ledger.acquire()
    assert ledger.peak_active_slots == 2
    assert ledger.agents_started == 3


def test_agent_cap_is_enforced():
    ledger = BudgetLedger(BudgetPolicy(max_agents=2, max_parallel_agents=10))
    ledger.acquire()
    ledger.release()
    ledger.acquire()
    ledger.release()
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.acquire()
    assert excinfo.value.reason == "agents"


def test_slot_context_manager_releases_on_error():
    ledger = BudgetLedger(BudgetPolicy(max_parallel_agents=1))
    with pytest.raises(RuntimeError):
        with ledger.slot():
            raise RuntimeError("boom")
    assert ledger.active_slots == 0


def test_snapshot_and_remaining_are_consistent():
    ledger = BudgetLedger.from_policy(Policy())
    ledger.charge(tokens=1000, cost=0.25, label="llm")
    snapshot = ledger.snapshot()
    assert snapshot["tokens"] == 1000
    assert snapshot["cost_usd"] == 0.25
    assert ledger.remaining()["agents"] == 12
    assert len(ledger.charges) == 1
