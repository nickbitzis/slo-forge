from __future__ import annotations

import pytest
from conftest import EXAMPLES

from sloforge.budget import SLOState, decide, load_policy, parse_policy
from sloforge.spec import SpecInvalid

POLICY = load_policy(EXAMPLES / "policy.yaml")


def st(b, page=False):
    return [SLOState("svc/availability", b, page)]


def test_example_policy_loads():
    assert [p.name for p in POLICY.policies] == ["freeze-features", "freeze-all-but-fixes",
                                                 "active-page"]


def test_healthy_budget_allows_everything():
    for change in ("feature", "config", "fix", "rollback"):
        assert decide(POLICY, "svc", "standard", change, st(0.6))["allowed"]


def test_low_budget_freezes_features_only():
    assert not decide(POLICY, "svc", "standard", "feature", st(0.2))["allowed"]
    assert decide(POLICY, "svc", "standard", "config", st(0.2))["allowed"]
    assert decide(POLICY, "svc", "standard", "fix", st(0.2))["allowed"]


def test_spent_budget_blocks_config_but_not_rollback():
    out = decide(POLICY, "svc", "standard", "config", st(-0.1))
    assert not out["allowed"]
    assert out["reasons"][0]["policy"] == "freeze-all-but-fixes"
    assert decide(POLICY, "svc", "standard", "rollback", st(-0.1))["allowed"]


def test_active_page_blocks_even_with_budget():
    out = decide(POLICY, "svc", "critical", "feature", st(0.9, page=True))
    assert not out["allowed"] and out["reasons"][0]["policy"] == "active-page"


def test_tier_scoped_rule():
    pol = parse_policy("""
apiVersion: sloforge/v1
kind: ErrorBudgetPolicy
policies:
  - {name: strict, when: {budget_remaining_below: 0.5}, block: [feature], tiers: [critical]}
""")
    assert not decide(pol, "a", "critical", "feature", st(0.4))["allowed"]
    assert decide(pol, "b", "low", "feature", st(0.4))["allowed"]


def test_policy_validation():
    with pytest.raises(SpecInvalid, match="exactly one"):
        parse_policy("""
apiVersion: sloforge/v1
kind: ErrorBudgetPolicy
policies:
  - {name: x, when: {}, block: [feature]}
""")
    with pytest.raises(SpecInvalid, match="block"):
        parse_policy("""
apiVersion: sloforge/v1
kind: ErrorBudgetPolicy
policies:
  - {name: x, when: {page_firing: true}, block: [deploy]}
""")


def test_unknown_change_type():
    with pytest.raises(ValueError, match="change must be"):
        decide(POLICY, "svc", "standard", "yolo", st(0.5))
