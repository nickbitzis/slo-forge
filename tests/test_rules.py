from __future__ import annotations

import json
import re

import pytest
import yaml
from conftest import BASE, EXAMPLES

from sloforge import promql, rules
from sloforge.spec import load_all, parse


@pytest.fixture(scope="module")
def specs():
    return load_all([EXAMPLES / "slos"])


@pytest.mark.parametrize("expr", [
    'sum(rate(http_requests_total{job="a",code=~"5.."}[5m])) / sum(rate(r_total{job="a"}[5m]))',
    '1 - (sum(rate(h_bucket{le="0.3"}[1h])) / sum(rate(h_count[1h])))',
    'a:b{x="1"} > (14.4 * 0.001) and a:c{x="1"} > (14.4 * 0.001)',
    '(a > 1 and b > 1) or (c > 2 and d > 2)',
    'sum by (job) (rate(x[5m]))',
    'x > bool 2',
    '-1 * 1e-05',
])
def test_parser_accepts(expr):
    assert promql.parse(expr) in ("instant", "scalar")


@pytest.mark.parametrize(("expr", "msg"), [
    ("rate(x)", "range vector"),
    ("sum(rate(x[5m])", "expected ')'"),
    ("x[5m] > 1", "range vector used"),
    ("frobnicate(x)", "unknown function"),
    ('x{code=~"5.("}', "bad regex"),
    ("x and 1", "set operator"),
    ('x{job=a}', "quoted value"),
    ("{}", "metric name or at least one matcher"),
    ("x > ", "unexpected"),
    ("x # y", "unexpected character"),
])
def test_parser_rejects(expr, msg):
    with pytest.raises(promql.PromQLError, match=re.escape(msg)):
        promql.parse(expr)


def test_every_generated_rule_parses(specs):
    for spec in specs:
        assert promql.check_rules(rules.recording_rules(spec)) == []
        for st in ("static", "single_window", "multiwindow"):
            assert promql.check_rules(rules.alerting_rules(spec, st)) == []


def test_check_rules_catches_structure_errors():
    data = {"groups": [
        {"name": "g", "rules": [{"record": "bad name", "expr": "x"},
                                {"record": "a", "alert": "B", "expr": "x"},
                                {"alert": "C", "expr": "rate(x[5m]", "for": "soon"}]},
        {"name": "g", "rules": []},
    ]}
    errs = promql.check_rules(data)
    text = " | ".join(errs)
    assert "invalid recording rule name" in text
    assert "exactly one of record or alert" in text
    assert "invalid for duration" in text
    assert "expected ')'" in text
    assert "duplicate group name" in text
    assert promql.check_rules({"nope": 1}) == ["top level must be a mapping with a 'groups' list"]


def test_recording_rules_cover_every_window(demo_spec):
    rec = rules.recording_rules(demo_spec)["groups"][0]["rules"]
    names = [r["record"] for r in rec]
    for w in ("5m", "30m", "1h", "6h", "3d", "30d"):
        assert f"sloforge:sli_error:ratio_rate{w}" in names
    assert rec[-1]["record"] == "sloforge:error_budget:remaining"
    assert rec[-1]["expr"].endswith("/ 0.001)")
    assert rec[0]["labels"] == {"service": "demo-api", "slo": "availability"}
    assert 'code=~"5.."' in rec[0]["expr"] and "[5m]" in rec[0]["expr"]


def test_multiwindow_alerts_match_workbook(demo_spec):
    group = rules.alerting_rules(demo_spec)["groups"][0]["rules"]
    page, ticket = group
    assert page["alert"] == "SLOBurnRatePage" and page["labels"]["severity"] == "page"
    assert "ratio_rate1h" in page["expr"] and "ratio_rate5m" in page["expr"]
    assert "(14.4 * 0.001)" in page["expr"] and "(6 * 0.001)" in page["expr"]
    assert " or " in page["expr"]
    assert ticket["labels"]["severity"] == "ticket"
    assert "ratio_rate3d" in ticket["expr"] and "(1 * 0.001)" in ticket["expr"]
    assert "for" not in page


def test_static_and_single_window_rules(demo_spec):
    static = rules.alerting_rules(demo_spec, "static")["groups"][0]["rules"][0]
    assert static["expr"].endswith("> 0.01") and static["for"] == "2m"
    single = rules.alerting_rules(demo_spec, "single_window")["groups"][0]["rules"][0]
    assert "ratio_rate1h" in single["expr"] and "ratio_rate5m" not in single["expr"]


def test_deployed_strategy_comes_from_spec(specs):
    reviews = next(s for s in specs if s.service == "reviews-api")
    g = rules.alerting_rules(reviews)["groups"][0]
    assert g["rules"][0]["alert"] == "SLOBurnRateSingleWindow"


def test_latency_sli_uses_histogram_bucket():
    spec = parse(BASE.replace("""      type: availability
      metric: http_requests_total
      labels: {job: demo-api}
      bad: {code: "5.."}""", """      type: latency
      metric: http_request_duration_seconds
      labels: {job: demo-api}
      threshold_seconds: 0.3"""))
    expr = rules.sli_error_expr(spec.slos[0], "5m")
    assert expr.startswith("1 - (")
    assert 'http_request_duration_seconds_bucket{job="demo-api",le="0.3"}[5m]' in expr
    assert promql.parse(expr) == "instant"


def test_num_never_uses_scientific_notation():
    assert rules.num(0.00001) == "0.00001"
    assert rules.num(14.4) == "14.4"
    assert rules.num(6.0) == "6"


def test_generate_writes_everything(specs, tmp_path):
    files = rules.generate(specs, tmp_path, every_strategy=True)
    assert len(files) == 15 * (1 + 3) + 15 * 2
    data = yaml.safe_load((tmp_path / "checkout-api.alerts.multiwindow.yaml").read_text())
    assert promql.check_rules(data) == []
    assert "&id" not in (tmp_path / "checkout-api.recording.yaml").read_text()
    dash = json.loads((tmp_path / "grafana" / "checkout-api.json").read_text())
    assert dash["uid"].startswith("sloforge-") and len(dash["panels"]) == 2 * 5
    docs = list(yaml.safe_load_all((tmp_path / "openslo" / "checkout-api.yaml").read_text()))
    assert [d["kind"] for d in docs] == ["Service", "SLO", "SLO"]
    slo = docs[1]["spec"]
    assert slo["objectives"][0]["target"] == 0.9995
    assert slo["timeWindow"] == [{"duration": "30d", "isRolling": True}]
    assert slo["budgetingMethod"] == "Occurrences"
