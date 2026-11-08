"""Worked examples from the SRE workbook, chapter 5 (Alerting on SLOs)."""

from __future__ import annotations

import pytest

from sloforge import burn

MONTH = 30 * 1440


def test_error_budget():
    assert burn.error_budget(99.9) == pytest.approx(0.001)
    assert burn.error_budget(99.95) == pytest.approx(0.0005)


def test_burn_rate_from_error_ratio():
    # 1% errors against a 99.9% SLO burns budget 10 times faster than sustainable.
    assert burn.burn_rate(0.01, 99.9) == pytest.approx(10)
    assert burn.burn_rate(0.001, 99.9) == pytest.approx(1)


@pytest.mark.parametrize(("consumed", "window", "expected"), [
    (0.02, 60, 14.4),        # 2% of budget in 1 hour: page
    (0.05, 360, 6),          # 5% in 6 hours: page
    (0.10, 3 * 1440, 1),     # 10% in 3 days: ticket
])
def test_workbook_burn_rate_table(consumed, window, expected):
    assert burn.burn_rate_for(consumed, window, MONTH) == pytest.approx(expected)


def test_budget_consumed_inverse():
    assert burn.budget_consumed(14.4, 60, MONTH) == pytest.approx(0.02)
    assert burn.budget_consumed(1, MONTH, MONTH) == pytest.approx(1)


def test_time_to_exhaustion():
    # At 14.4x a 30 day budget lasts 50 hours.
    assert burn.time_to_exhaustion_min(14.4, MONTH) == pytest.approx(50 * 60)
    assert burn.time_to_exhaustion_min(0, MONTH) == float("inf")


def test_detection_time():
    # Full outage on a 99.9% SLO (burn 1000) crosses 14.4x over 1h in 0.864 minutes.
    assert burn.detection_time_min(1000, 14.4, 60) == pytest.approx(0.864)
    # A 10x burn never reaches a 14.4x threshold.
    assert burn.detection_time_min(10, 14.4, 60) == float("inf")


def test_budget_remaining():
    assert burn.budget_remaining(50, 100_000, 99.9) == pytest.approx(0.5)
    assert burn.budget_remaining(200, 100_000, 99.9) == pytest.approx(-1)
    assert burn.budget_remaining(0, 0, 99.9) == 1.0
