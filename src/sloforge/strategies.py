"""Alert strategies as data. The rule generator renders them to PromQL and the evaluator
replays them over simulated streams, so what is scored is exactly what gets deployed."""

from __future__ import annotations

from dataclasses import dataclass

from sloforge.spec import SLO, minutes

STRATEGIES = ("static", "single_window", "multiwindow")
LABELS = {
    "static": "Static error rate threshold",
    "single_window": "Single-window burn rate",
    "multiwindow": "Multiwindow multi-burn-rate",
}


@dataclass(frozen=True)
class Condition:
    """True when the error ratio over every window is above `threshold`."""

    windows: tuple[str, ...]
    threshold: float
    burn_rate: float | None = None  # set when the threshold is burn_rate * budget


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str  # page or ticket
    any_of: tuple[Condition, ...]  # OR of conditions
    for_: str = "0m"

    @property
    def for_minutes(self) -> int:
        return 0 if self.for_ == "0m" else minutes(self.for_)


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    rules: tuple[AlertRule, ...]

    def windows(self) -> set[str]:
        return {w for r in self.rules for c in r.any_of for w in c.windows}


def build(slo: SLO, key: str) -> Strategy:
    a, budget = slo.alerting, slo.budget
    if key == "static":
        rules = (AlertRule("SLOStaticErrorRate", "page",
                           (Condition(("5m",), slo.static_threshold),), a.static_for),)
    elif key == "single_window":
        sw = a.single_window
        rules = (AlertRule("SLOBurnRateSingleWindow", "page",
                           (Condition((sw.window,), sw.burn_rate * budget, sw.burn_rate),)),)
    elif key == "multiwindow":
        out = []
        for sev, name in (("page", "SLOBurnRatePage"), ("ticket", "SLOBurnRateTicket")):
            conds = tuple(Condition((w.long, w.short), w.burn_rate * budget, w.burn_rate)
                          for w in a.multiwindow if w.severity == sev)
            if conds:
                out.append(AlertRule(name, sev, conds))
        rules = tuple(out)
    else:
        raise ValueError(f"unknown strategy {key!r}")
    return Strategy(key, LABELS[key], rules)


def all_strategies(slo: SLO) -> dict[str, Strategy]:
    return {k: build(slo, k) for k in STRATEGIES}


def windows_needed(slo: SLO) -> list[str]:
    ws = {w for s in all_strategies(slo).values() for w in s.windows()} | {slo.window}
    return sorted(ws, key=minutes)
