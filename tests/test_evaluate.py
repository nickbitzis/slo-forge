"""Alert strategies on hand-built streams, and precision and recall accounting."""

from __future__ import annotations

import numpy as np
import pytest

from sloforge import burn
from sloforge.evaluate import (
    Episode,
    episodes,
    evaluate_stream,
    for_filter,
    score,
    window_sum,
)
from sloforge.simulator import DAY, Incident, Stream
from sloforge.strategies import all_strategies

N = 10 * DAY
WARM = 3 * DAY
TOTAL = 1000


def stream(demo_spec, segments, base=0.0002, inc_kind="x", significant=True):
    """Constant 1000 requests a minute; segments are (start, minutes, error_ratio)."""
    total = np.full(N, TOTAL, dtype=np.int64)
    ratio = np.full(N, base)
    incidents = []
    for k, (s, m, r) in enumerate(segments):
        ratio[s:s + m] = r
        incidents.append(Incident(f"demo#{k}", "demo-api/availability", "demo-api", inc_kind,
                                  s, s + m, r / 0.001, significant=significant))
    bad = np.round(total * ratio).astype(np.int64)
    return Stream("demo-api/availability", "demo-api", "availability", "availability", 99.9,
                  0.001, 30 * DAY, total, bad, incidents)


def run(demo_spec, st):
    return evaluate_stream(st, all_strategies(demo_spec.slos[0]), WARM)


def test_window_sum():
    x = np.array([1, 2, 3, 4])
    assert window_sum(x, 2).tolist() == [1, 3, 5, 7]
    assert window_sum(x, 10).tolist() == [1, 3, 6, 10]


def test_for_filter_matches_prometheus():
    cond = np.array([0, 1, 1, 1, 0, 1, 1, 1, 1], dtype=bool)
    # for: 2m fires at the third consecutive true evaluation.
    assert for_filter(cond, 2).astype(int).tolist() == [0, 0, 0, 1, 0, 0, 0, 1, 1]
    assert np.array_equal(for_filter(cond, 0), cond)


def test_episodes_merge_short_gaps():
    f = np.array([0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 1], dtype=bool)
    assert episodes(f) == [(1, 3), (5, 6), (10, 11)]
    assert episodes(f, merge_gap=3) == [(1, 6), (10, 11)]
    assert episodes(f, start=4) == [(5, 6), (10, 11)]


def test_quiet_stream_never_alerts(demo_spec):
    out = run(demo_spec, stream(demo_spec, []))
    assert all(v == [] for v in out.values())


def test_blip_pages_static_only(demo_spec):
    # 5% errors for 4 minutes: 50x burn, 0.46% of the budget.
    out = run(demo_spec, stream(demo_spec, [(WARM + DAY, 4, 0.05)], significant=False))
    assert len(out["static"]) == 1 and not out["static"][0].true
    assert out["single_window"] == [] and out["multiwindow"] == []


def test_slow_burn_missed_by_static_caught_by_ticket(demo_spec):
    # 0.4% errors (4x burn) for 2 days spends 27% of the budget.
    start = WARM + DAY
    out = run(demo_spec, stream(demo_spec, [(start, 2 * DAY, 0.004)]))
    assert out["static"] == [] and out["single_window"] == []
    pages = [e for e in out["multiwindow"] if e.severity == "page"]
    tickets = [e for e in out["multiwindow"] if e.severity == "ticket"]
    assert pages == [] and len(tickets) == 1 and tickets[0].true
    # Baseline rounds to 0 errors at 1000 rpm, so the 3d average reaches 1x after
    # 3 days / 4 = 18 hours, as burn.detection_time_min predicts.
    delay = tickets[0].start - start
    assert delay == pytest.approx(burn.detection_time_min(4, 1, 3 * DAY), abs=2)


def test_medium_burn_pages_through_six_hour_window(demo_spec):
    # 0.8% errors (8x) for 8 hours: the 6h window crosses 6x after about 4.5 hours.
    start = WARM + DAY
    out = run(demo_spec, stream(demo_spec, [(start, 8 * 60, 0.008)]))
    page = next(e for e in out["multiwindow"] if e.severity == "page")
    assert 4 * 60 < page.start - start < 5 * 60
    assert out["single_window"] == []


def test_outage_detection_and_reset(demo_spec):
    start, length = WARM + DAY, 30
    out = run(demo_spec, stream(demo_spec, [(start, length, 1.0)]))
    static, single = out["static"][0], out["single_window"][0]
    page = next(e for e in out["multiwindow"] if e.severity == "page")
    assert static.start - start == 2  # for: 2m
    assert single.start - start == 0 and page.start - start == 0
    end = start + length
    # The 1h window keeps the single-window alert firing for most of an hour after recovery.
    assert 55 <= single.end - end <= 60
    # The page has two conditions. 14.4x over 1h AND 5m clears within 5 minutes, but
    # 6x over 6h AND 30m holds until the 30m window drains: the short window sets the reset.
    assert 25 <= page.end - end <= 30 < single.end - end
    assert static.end - end <= 5


def test_score_accounting():
    incs = [Incident("a", "s", "svc", "deploy_spike", 100, 130, 50, 0.03, True),
            Incident("b", "s", "svc", "blip", 500, 503, 100, 0.005, False),
            Incident("c", "s", "svc", "slow_burn", 1000, 3000, 3, 0.2, True)]
    eps = [Episode("s", "x", "page", 110, 150, "a", True),
           Episode("s", "x", "page", 160, 170, "a", True),
           Episode("s", "x", "page", 501, 505, "b", False),
           Episode("s", "x", "page", 900, 905, None, False)]
    s = score(eps, incs, days=7)
    assert s["notifications"] == 4 and s["true"] == 2 and s["false"] == 2
    assert s["false_on_minor_incidents"] == 1 and s["false_on_noise"] == 1
    assert s["precision"] == pytest.approx(0.5)
    assert s["recall"] == pytest.approx(0.5) and s["detected"] == 1
    assert s["detections"] == {"a": 10} and s["resets"] == {"a": 40}
    assert s["per_week"] == 4 and s["false_per_week"] == 2


def test_attribution_window(demo_spec):
    st = stream(demo_spec, [(WARM + DAY, 20, 0.05)])
    out = run(demo_spec, st)
    assert out["static"][0].incident == "demo#0" and out["static"][0].true
