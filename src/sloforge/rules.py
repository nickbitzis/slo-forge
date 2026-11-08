"""Generators: Prometheus recording and alerting rules, Grafana dashboards, OpenSLO export."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from sloforge import promql
from sloforge.spec import SLO, AvailabilitySLI, ServiceSpec
from sloforge.strategies import (
    STRATEGIES,
    AlertRule,
    Condition,
    all_strategies,
    build,
    windows_needed,
)

PREFIX = "sloforge"
RUNBOOK = "https://runbooks.example.com/slo"


def num(x: float) -> str:
    """Plain decimal, never scientific notation: 0.00001 -> '0.00001'."""
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s or "0"


def record_name(window: str) -> str:
    return f"{PREFIX}:sli_error:ratio_rate{window}"


def _matchers(labels: dict[str, str], regex: dict[str, str] | None = None,
              extra: dict[str, str] | None = None) -> str:
    parts = [f'{k}="{v}"' for k, v in labels.items()]
    parts += [f'{k}=~"{v}"' for k, v in (regex or {}).items()]
    parts += [f'{k}="{v}"' for k, v in (extra or {}).items()]
    return "{" + ",".join(parts) + "}"


def sli_error_expr(slo: SLO, window: str) -> str:
    """Error ratio of the SLI over a window, from the raw metrics."""
    s = slo.sli
    if isinstance(s, AvailabilitySLI):
        bad = f"sum(rate({s.metric}{_matchers(s.labels, s.bad)}[{window}]))"
        total = f"sum(rate({s.metric}{_matchers(s.labels)}[{window}]))"
        return f"{bad} / {total}"
    le = repr(float(s.threshold_seconds))
    good = f"sum(rate({s.metric}_bucket{_matchers(s.labels, extra={'le': le})}[{window}]))"
    total = f"sum(rate({s.metric}_count{_matchers(s.labels)}[{window}]))"
    return f"1 - ({good} / {total})"


def _sel(spec: ServiceSpec, slo: SLO) -> str:
    return f'{{service="{spec.service}",slo="{slo.name}"}}'


def condition_expr(spec: ServiceSpec, slo: SLO, c: Condition) -> str:
    thr = f"({num(c.burn_rate)} * {num(slo.budget)})" if c.burn_rate else num(c.threshold)
    return " and ".join(f"{record_name(w)}{_sel(spec, slo)} > {thr}" for w in c.windows)


def alert_expr(spec: ServiceSpec, slo: SLO, rule: AlertRule) -> str:
    parts = [condition_expr(spec, slo, c) for c in rule.any_of]
    if len(parts) == 1:
        return parts[0]
    return " or ".join(f"({p})" for p in parts)


def _describe(slo: SLO, rule: AlertRule) -> str:
    bits = []
    for c in rule.any_of:
        if c.burn_rate:
            bits.append(f"burn rate above {num(c.burn_rate)}x over {' and '.join(c.windows)}")
        else:
            bits.append(f"error ratio above {num(c.threshold)} over {c.windows[0]}")
    return "; or ".join(bits)


def recording_group(spec: ServiceSpec, slo: SLO) -> dict:
    labels = {"service": spec.service, "slo": slo.name}
    rules = [{"record": record_name(w), "expr": sli_error_expr(slo, w), "labels": labels}
             for w in windows_needed(slo)]
    rules.append({
        "record": f"{PREFIX}:error_budget:remaining",
        "expr": f"1 - ({record_name(slo.window)}{_sel(spec, slo)} / {num(slo.budget)})",
        "labels": labels,
    })
    return {"name": f"{PREFIX}-{spec.service}-{slo.name}-recording", "interval": "30s",
            "rules": rules}


def alert_group(spec: ServiceSpec, slo: SLO, strategy: str) -> dict:
    st = build(slo, strategy)
    rules = []
    for r in st.rules:
        rule: dict = {"alert": r.name, "expr": alert_expr(spec, slo, r)}
        if r.for_minutes:
            rule["for"] = r.for_
        rule["labels"] = {"severity": r.severity, "service": spec.service, "slo": slo.name,
                          "owner": spec.owner, "strategy": strategy}
        rule["annotations"] = {
            "summary": f"{spec.service} {slo.name} SLO ({num(slo.objective)}% over "
                       f"{slo.window}) is burning error budget",
            "description": f"{_describe(slo, r)}. Current value {{{{ $value }}}}.",
            "runbook_url": f"{RUNBOOK}/{spec.service}/{slo.name}",
        }
        rules.append(rule)
    return {"name": f"{PREFIX}-{spec.service}-{slo.name}-{strategy}", "rules": rules}


def recording_rules(spec: ServiceSpec) -> dict:
    return {"groups": [recording_group(spec, s) for s in spec.slos]}


def alerting_rules(spec: ServiceSpec, strategy: str | None = None) -> dict:
    return {"groups": [alert_group(spec, s, strategy or s.alerting.strategy) for s in spec.slos]}


class _NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def dump_yaml(data: object) -> str:
    return yaml.dump(data, Dumper=_NoAliases, sort_keys=False, width=1000)


def grafana_dashboard(spec: ServiceSpec) -> dict:
    """An illustrative dashboard: budget remaining, then one panel per burn rate window."""
    panels, y, pid = [], 0, 1
    for slo in spec.slos:
        sel = _sel(spec, slo)
        panels.append({"id": pid, "type": "stat", "title": f"{slo.name}: budget remaining",
                       "gridPos": {"x": 0, "y": y, "w": 6, "h": 6},
                       "fieldConfig": {"defaults": {"unit": "percentunit", "thresholds": {
                           "mode": "absolute", "steps": [
                               {"color": "red", "value": None},
                               {"color": "orange", "value": 0.25},
                               {"color": "green", "value": 0.5}]}}},
                       "targets": [{"refId": "A",
                                    "expr": f"{PREFIX}:error_budget:remaining{sel}"}]})
        pid += 1
        for i, w in enumerate(windows_needed(slo)[:4]):
            panels.append({
                "id": pid, "type": "timeseries", "title": f"{slo.name}: burn rate over {w}",
                "gridPos": {"x": 6 + i * 4, "y": y, "w": 4 if i < 3 else 6, "h": 6},
                "fieldConfig": {"defaults": {"unit": "x"}},
                "targets": [{"refId": "A",
                             "expr": f"{record_name(w)}{sel} / {num(slo.budget)}",
                             "legendFormat": f"{w} burn rate"}]})
            pid += 1
        y += 6
    return {"title": f"SLOs: {spec.service}", "uid": f"sloforge-{spec.service}"[:40],
            "tags": ["slo", "sloforge", spec.tier], "schemaVersion": 39, "version": 1,
            "time": {"from": "now-7d", "to": "now"}, "refresh": "1m", "panels": panels}


def _openslo_queries(slo: SLO) -> tuple[str, str]:
    s = slo.sli
    if isinstance(s, AvailabilitySLI):
        good = f"sum(rate({s.metric}{_matchers(s.labels)}[1m])) - " \
               f"sum(rate({s.metric}{_matchers(s.labels, s.bad)}[1m]))"
        return good, f"sum(rate({s.metric}{_matchers(s.labels)}[1m]))"
    le = repr(float(s.threshold_seconds))
    return (f"sum(rate({s.metric}_bucket{_matchers(s.labels, extra={'le': le})}[1m]))",
            f"sum(rate({s.metric}_count{_matchers(s.labels)}[1m]))")


def openslo(spec: ServiceSpec) -> list[dict]:
    docs: list[dict] = [{"apiVersion": "openslo/v1", "kind": "Service",
                         "metadata": {"name": spec.service, "displayName": spec.service},
                         "spec": {"description": spec.description or spec.service}}]
    for slo in spec.slos:
        good, total = _openslo_queries(slo)
        docs.append({
            "apiVersion": "openslo/v1", "kind": "SLO",
            "metadata": {"name": f"{spec.service}-{slo.name}",
                         "labels": {"owner": [spec.owner], "tier": [spec.tier]}},
            "spec": {
                "description": slo.description or f"{slo.sli.type} SLO for {spec.service}",
                "service": spec.service,
                "indicator": {"metadata": {"name": f"{spec.service}-{slo.name}-sli"},
                              "spec": {"ratioMetric": {
                                  "counter": True,
                                  "good": {"metricSource": {"type": "Prometheus",
                                                            "spec": {"query": good}}},
                                  "total": {"metricSource": {"type": "Prometheus",
                                                             "spec": {"query": total}}}}}},
                "timeWindow": [{"duration": slo.window, "isRolling": True}],
                "budgetingMethod": "Occurrences",
                "objectives": [{"displayName": slo.name,
                                "target": round(slo.objective / 100, 6)}],
            },
        })
    return docs


def generate(specs: list[ServiceSpec], out: Path, every_strategy: bool = False) -> list[Path]:
    """Write all artifacts. Returns the files written. Raises ValueError on a bad rule."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "grafana").mkdir(exist_ok=True)
    (out / "openslo").mkdir(exist_ok=True)
    written: list[Path] = []

    def write(path: Path, text: str) -> None:
        path.write_text(text)
        written.append(path)

    for spec in specs:
        rec = recording_rules(spec)
        files = {out / f"{spec.service}.recording.yaml": rec}
        if every_strategy:
            for st in STRATEGIES:
                files[out / f"{spec.service}.alerts.{st}.yaml"] = alerting_rules(spec, st)
        else:
            files[out / f"{spec.service}.alerts.yaml"] = alerting_rules(spec)
        for path, data in files.items():
            errs = promql.check_rules(data)
            if errs:
                raise ValueError(f"{path.name}: " + "; ".join(errs))
            write(path, dump_yaml(data))
        write(out / "grafana" / f"{spec.service}.json",
              json.dumps(grafana_dashboard(spec), indent=2) + "\n")
        write(out / "openslo" / f"{spec.service}.yaml", yaml.dump_all(
            openslo(spec), Dumper=_NoAliases, sort_keys=False, width=1000))
    return written


__all__ = ["generate", "recording_rules", "alerting_rules", "grafana_dashboard", "openslo",
           "all_strategies"]
