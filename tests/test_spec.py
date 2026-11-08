from __future__ import annotations

import pytest
from conftest import BASE, EXAMPLES

from sloforge.spec import SpecInvalid, json_schema, load_all, minutes, parse


def errors(text: str) -> list[str]:
    with pytest.raises(SpecInvalid) as e:
        parse(text, "x.yaml")
    return [str(x) for x in e.value.errors]


def test_examples_are_valid():
    specs = load_all([EXAMPLES / "slos"])
    assert len(specs) == 15
    assert sum(len(s.slos) for s in specs) == 22


def test_base_spec_defaults(demo_spec):
    slo = demo_spec.slos[0]
    assert slo.budget == pytest.approx(0.001)
    assert slo.window_minutes == 30 * 1440
    assert slo.static_threshold == pytest.approx(0.01)
    assert [w.burn_rate for w in slo.alerting.multiwindow] == [14.4, 6, 1]
    assert slo.alerting.strategy == "multiwindow"


def test_objective_out_of_range_has_path():
    errs = errors(BASE.replace("99.9", "100.5"))
    assert errs == ["x.yaml: slos[0].objective: Input should be less than 100"]


def test_too_many_nines():
    assert "26 seconds" in errors(BASE.replace("99.9", "99.9999"))[0]


def test_window_must_be_28_or_30_days():
    assert "slos[0].window" in errors(BASE.replace("30d", "7d"))[0]


def test_missing_bad_matcher():
    errs = errors(BASE.replace('      bad: {code: "5.."}\n', ""))
    assert errs == ["x.yaml: slos[0].sli.bad: Field required"]


def test_bad_service_name():
    assert "service:" in errors(BASE.replace("service: demo-api", "service: Demo_API"))[0]


def test_unknown_field_rejected():
    errs = errors(BASE.replace("owner:", "owners:"))
    assert "x.yaml: owners: Extra inputs are not permitted" in errs


def test_duplicate_slo_names():
    text = BASE + BASE.split("slos:\n")[1]
    assert "duplicate SLO name 'availability'" in errors(text)[0]


def test_impossible_burn_rate():
    errs = errors(BASE.replace("99.9", "90"))
    assert "can never fire" in errs[0] and "is 10" in errs[0]


def test_short_window_must_be_shorter():
    text = BASE + """    alerting:
      multiwindow:
        - {long: 1h, short: 2h, burn_rate: 14.4, severity: page}
"""
    assert "must be shorter than long 1h" in errors(text)[0]


def test_static_threshold_below_budget():
    text = BASE + "    alerting: {static_threshold: 0.0005}\n"
    assert "would page while the SLO is being met" in errors(text)[0]


def test_window_longer_than_slo_window():
    text = BASE + """    alerting:
      multiwindow:
        - {long: 40d, short: 1d, burn_rate: 1, severity: ticket}
"""
    assert "longer than the SLO window" in errors(text)[0]


def test_latency_metric_suffix():
    text = BASE.replace("""      type: availability
      metric: http_requests_total
      labels: {job: demo-api}
      bad: {code: "5.."}""", """      type: latency
      metric: http_request_duration_seconds_bucket
      threshold_seconds: 0.3""")
    assert "without '_bucket'" in errors(text)[0]


def test_yaml_syntax_error_reports_line():
    errs = errors("apiVersion: sloforge/v1\nservice: [unclosed\n")
    assert "YAML syntax error" in errs[0] and "line" in errs[0]


def test_top_level_must_be_mapping():
    assert "mapping" in errors("- a\n- b\n")[0]


def test_invalid_examples_report_every_error():
    with pytest.raises(SpecInvalid) as e:
        load_all([EXAMPLES / "invalid"])
    assert len(e.value.errors) == 5


def test_duplicate_service_across_files(tmp_path):
    (tmp_path / "a.yaml").write_text(BASE)
    (tmp_path / "b.yaml").write_text(BASE)
    with pytest.raises(SpecInvalid) as e:
        load_all([tmp_path])
    assert "also defined in" in str(e.value)


def test_durations():
    assert minutes("5m") == 5 and minutes("6h") == 360 and minutes("3d") == 4320
    with pytest.raises(ValueError):
        minutes("0m")
    with pytest.raises(ValueError):
        minutes("1w")


def test_json_schema_exports():
    s = json_schema()
    assert "slos" in s["properties"] and "SLO" in s["$defs"]
