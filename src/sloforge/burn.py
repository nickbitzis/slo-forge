"""Error budget and burn rate math (Google SRE workbook, chapter 5).

budget          = 1 - objective                      (allowed error ratio)
burn rate       = observed error ratio / budget      (1.0 spends the budget exactly over the window)
budget consumed = burn rate * alert window / SLO window
"""

from __future__ import annotations


def error_budget(objective_pct: float) -> float:
    return round(1 - objective_pct / 100, 12)


def burn_rate(error_ratio: float, objective_pct: float) -> float:
    return error_ratio / error_budget(objective_pct)


def budget_consumed(burn: float, alert_window_min: float, slo_window_min: float) -> float:
    """Fraction of the whole budget spent by burning at `burn` for `alert_window_min`."""
    return burn * alert_window_min / slo_window_min


def burn_rate_for(consumed: float, alert_window_min: float, slo_window_min: float) -> float:
    """Burn rate that spends `consumed` of the budget within the alert window.

    2% in 1 hour of a 30 day window -> 14.4. 5% in 6 hours -> 6. 10% in 3 days -> 1.
    """
    return consumed * slo_window_min / alert_window_min


def time_to_exhaustion_min(burn: float, slo_window_min: float) -> float:
    """Minutes until a full budget is gone at a constant burn rate."""
    return float("inf") if burn <= 0 else slo_window_min / burn


def detection_time_min(burn: float, threshold: float, alert_window_min: float) -> float:
    """Minutes from the start of a step change until the window average crosses the threshold.

    Assumes no errors before the step. The window average after t minutes is burn * t / window.
    """
    if burn <= threshold:
        return float("inf")
    return threshold * alert_window_min / burn


def budget_remaining(bad: float, total: float, objective_pct: float) -> float:
    """1.0 means untouched, 0 means spent, negative means the SLO is breached."""
    if total <= 0:
        return 1.0
    allowed = error_budget(objective_pct) * total
    return 1 - bad / allowed
