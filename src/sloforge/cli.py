"""sloforge command line: validate, generate, check-rules, evaluate, serve, gate, healthcheck."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from sloforge import promql
from sloforge.budget import CHANGE_TYPES, load_policy
from sloforge.spec import SpecInvalid, json_schema, load_all, spec_files


def cmd_validate(a: argparse.Namespace) -> int:
    try:
        specs = load_all([Path(p) for p in a.paths])
        if a.policy:
            load_policy(Path(a.policy))
    except SpecInvalid as e:
        for err in e.errors:
            print(f"error: {err}", file=sys.stderr)
        print(f"{len(e.errors)} error(s)", file=sys.stderr)
        return 1
    n = sum(len(s.slos) for s in specs)
    print(f"ok: {len(specs)} services, {n} SLOs"
          + (f", policy {a.policy}" if a.policy else ""))
    return 0


def cmd_generate(a: argparse.Namespace) -> int:
    from sloforge.rules import generate

    try:
        specs = load_all([Path(p) for p in a.paths])
        files = generate(specs, Path(a.out), every_strategy=a.all_strategies)
    except SpecInvalid as e:
        for err in e.errors:
            print(f"error: {err}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: generated rules failed the check: {e}", file=sys.stderr)
        return 1
    print(f"wrote {len(files)} files to {a.out}")
    return 0


def cmd_check_rules(a: argparse.Namespace) -> int:
    bad = 0
    files = spec_files([Path(p) for p in a.paths])
    for f in files:
        errs = promql.check_rules(yaml.safe_load(f.read_text()))
        for e in errs:
            print(f"error: {f}: {e}", file=sys.stderr)
        bad += len(errs)
    print(f"checked {len(files)} rule files, {bad} error(s)")
    return 1 if bad or not files else 0


def cmd_schema(_a: argparse.Namespace) -> int:
    print(json.dumps(json_schema(), indent=2))
    return 0


def _table(ev: dict) -> str:
    def f(v, pct=False):
        if v is None:
            return "n/a"
        return f"{v:.0%}" if pct else f"{v:g}"

    lines = ["| Strategy | Notifications | False | Precision | Recall | Median time to detect "
             "(min) | Median reset after recovery (min) | Per week |",
             "|---|---|---|---|---|---|---|---|"]
    for r in ev["rows"]:
        lines.append(f"| {r['label']} | {r['notifications']} | {r['false']} | "
                     f"{f(r['precision'], True)} | {f(r['recall'], True)} "
                     f"({r['detected']}/{r['significant_incidents']}) | "
                     f"{f(r['median_detection_min'])} | {f(r['median_reset_min'])} | "
                     f"{r['per_week']} |")
    return "\n".join(lines)


def _kinds_table(ev: dict) -> str:
    keys = ["static+page", "single_window+page", "multiwindow+page", "multiwindow+page+ticket"]
    lines = ["| Incident kind | Count | Budget-threatening | Median budget spent | Static | "
             "Single window | MWMBR pages | MWMBR pages + tickets |",
             "|---|---|---|---|---|---|---|---|"]
    for k in ev["kinds"]:
        cells = []
        for key in keys:
            b = k["by_row"][key]
            if k["significant"]:
                med = b["median_detection_min"]
                cells.append(f"{b['detected']}/{b['of']}" + (f", {med:g} min" if med else ""))
            else:
                cells.append(f"{b['notifications']} false")
        lines.append(f"| {k['kind']} | {k['count']} | {k['significant']} | "
                     f"{k['median_budget_consumed']:.2%} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def cmd_evaluate(a: argparse.Namespace) -> int:
    from sloforge.engine import Engine
    from sloforge.simulator import SimConfig

    e = Engine.from_paths(Path(a.specs), Path(a.policy), SimConfig(seed=a.seed))
    ev = e.evaluation()
    gates = [e.gate(s, "feature") for s in e.specs]
    if a.out:
        out = {**ev, "gates_at_end": [{k: g[k] for k in ("service", "allowed", "reasons")}
                                      for g in gates]}
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(f"{ev['services']} services, {ev['slos']} SLOs, {ev['days']} days, seed {ev['seed']}, "
          f"{len(ev['incidents'])} incidents ({sum(i['significant'] for i in ev['incidents'])} "
          f"budget-threatening), built in {e.build_seconds} s\n")
    print(_table(ev))
    print()
    print(_kinds_table(ev))
    blocked = [g["service"] for g in gates if not g["allowed"]]
    print(f"\nFeature deploys blocked at day 90: {', '.join(blocked) or 'none'}")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from sloforge.api import create_app

    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_gate(a: argparse.Namespace) -> int:
    q = {"service": a.service, "change": a.change}
    if a.day is not None:
        q["day"] = str(a.day)
    url = f"{a.url.rstrip('/')}/api/deploy-gate?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            out = json.load(r)
    except urllib.error.URLError as e:
        print(f"error: gate unreachable: {e}", file=sys.stderr)
        return 2
    if out["allowed"]:
        print(f"allowed: {a.change} deploy of {a.service}")
        return 0
    print(f"blocked: {a.change} deploy of {a.service}")
    for r in out["reasons"]:
        print(f"  {r['policy']}: {r['slo_id']} budget remaining {r['budget_remaining']:.1%}. "
              f"{r['description']}")
    return 1


def cmd_healthcheck(a: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(a.url, timeout=3) as r:
            return 0 if json.load(r).get("status") == "ok" else 1
    except (urllib.error.URLError, ValueError, OSError):
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sloforge", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="check SLO specs (and optionally a policy) against "
                                        "the schema")
    p.add_argument("paths", nargs="+", help="spec files or directories")
    p.add_argument("--policy", help="error budget policy file")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("generate", help="write Prometheus rules, Grafana, and OpenSLO files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--out", default="out")
    p.add_argument("--all-strategies", action="store_true",
                   help="one alert file per strategy instead of only the deployed one")
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("check-rules", help="parse and check Prometheus rule files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_check_rules)

    p = sub.add_parser("schema", help="print the spec JSON Schema")
    p.set_defaults(fn=cmd_schema)

    p = sub.add_parser("evaluate", help="simulate 90 days and score every alert strategy")
    p.add_argument("--specs", default="examples/slos")
    p.add_argument("--policy", default="examples/policy.yaml")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", help="write results JSON here")
    p.set_defaults(fn=cmd_evaluate)

    p = sub.add_parser("serve", help="API and web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8835)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("gate", help="ask a running server whether a deploy is allowed; "
                                    "exit 1 if blocked")
    p.add_argument("service")
    p.add_argument("--change", choices=CHANGE_TYPES, default="feature")
    p.add_argument("--day", type=float)
    p.add_argument("--url", default="http://127.0.0.1:8835")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("healthcheck", help="exit 0 if the server answers /healthz")
    p.add_argument("--url", default="http://127.0.0.1:8835/healthz")
    p.set_defaults(fn=cmd_healthcheck)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
