from __future__ import annotations

import numpy as np
from conftest import EXAMPLES

from sloforge.simulator import DAY, SIGNIFICANT, SimConfig, simulate, simulate_slo, traffic
from sloforge.spec import load_all

SMALL = SimConfig(seed=3, days=20, warmup_days=3)


def specs():
    return [s for s in load_all([EXAMPLES / "slos"]) if s.service in ("checkout-api", "search-api")]


def test_same_seed_is_identical():
    a, b = simulate(specs(), SMALL), simulate(specs(), SMALL)
    for x, y in zip(a, b, strict=True):
        assert np.array_equal(x.total, y.total) and np.array_equal(x.bad, y.bad)
        assert [i.start for i in x.incidents] == [i.start for i in y.incidents]


def test_different_seed_differs():
    a = simulate(specs(), SMALL)[0]
    b = simulate(specs(), SimConfig(seed=4, days=20, warmup_days=3))[0]
    assert not np.array_equal(a.bad, b.bad)


def test_traffic_has_a_daily_cycle():
    t = traffic("checkout-api", SMALL)
    hour = (np.arange(len(t)) % DAY) // 60
    assert t[hour == 16].mean() > 3 * t[hour == 4].mean()


def test_scripted_outage_is_injected():
    cfg = SimConfig(seed=7, days=90)
    spec = next(s for s in specs() if s.service == "checkout-api")
    total = traffic(spec.service, cfg)
    st = simulate_slo(spec, spec.slos[0], total, cfg)
    outage = next(i for i in st.incidents if i.kind == "outage")
    assert outage.start == cfg.warmup + int(41.40 * DAY) and outage.minutes == 26
    assert (st.bad[outage.start:outage.end] == st.total[outage.start:outage.end]).all()
    assert outage.significant and outage.budget_consumed > 1.0


def test_incidents_raise_errors_and_do_not_overlap():
    for st in simulate(specs(), SMALL):
        base = st.bad.sum() / st.total.sum()
        spans = sorted((i.start, i.end) for i in st.incidents)
        for (_, e1), (s2, _) in zip(spans, spans[1:], strict=False):
            assert s2 > e1
        for i in st.incidents:
            assert i.start >= SMALL.warmup
            seg = st.bad[i.start:i.end].sum() / max(1, st.total[i.start:i.end].sum())
            if i.burn * st.budget > 5 * base:
                assert seg > base


def test_significance_flag_matches_budget_spent():
    for st in simulate(specs(), SMALL):
        for i in st.incidents:
            assert i.significant == (i.budget_consumed >= SIGNIFICANT)
            assert i.id.startswith(st.slo_id + "#")


def test_baseline_is_inside_the_budget():
    cfg = SimConfig(seed=1, days=10, warmup_days=1, random_incidents=False, scripted=False)
    for st in simulate(specs(), cfg):
        ratio = st.bad.sum() / st.total.sum()
        assert 0.05 * st.budget < ratio < 0.6 * st.budget
        assert st.incidents == []
