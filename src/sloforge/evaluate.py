"""Replay streams minute by minute through each alert strategy and score against ground truth.

Matching: an alert episode belongs to an incident on the same SLO when it starts between the
incident start and 60 minutes after its end. Episodes that match a budget-threatening incident
are true; episodes that match nothing, or only a minor incident such as a short blip, are false.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from sloforge.simulator import DAY, Incident, Stream
from sloforge.spec import minutes
from sloforge.strategies import STRATEGIES, AlertRule, Strategy

GRACE = 60
# An alert that resolves and fires again within this many minutes is the same page: the
# incident is still open in the paging tool, and Alertmanager groups the notifications.
MERGE_GAP = 60


def window_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Sum over the trailing w minutes, ending at and including each minute."""
    c = np.concatenate(([0], np.cumsum(x)))
    idx = np.arange(1, len(x) + 1)
    return c[idx] - c[np.maximum(idx - w, 0)]


def error_ratio(bad: np.ndarray, total: np.ndarray, w: int) -> np.ndarray:
    b, t = window_sum(bad, w), window_sum(total, w)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(t > 0, b / np.maximum(t, 1), 0.0)


class RatioCache:
    def __init__(self, stream: Stream) -> None:
        self.s = stream
        self.cache: dict[str, np.ndarray] = {}

    def get(self, window: str) -> np.ndarray:
        if window not in self.cache:
            self.cache[window] = error_ratio(self.s.bad, self.s.total, minutes(window))
        return self.cache[window]


def for_filter(cond: np.ndarray, for_min: int) -> np.ndarray:
    """Prometheus `for`: firing once the condition has held at for_min + 1 evaluations."""
    if for_min <= 0:
        return cond
    idx = np.arange(len(cond))
    last_false = np.maximum.accumulate(np.where(~cond, idx, -1))
    return (idx - last_false) >= for_min + 1


def rule_firing(rule: AlertRule, ratios: RatioCache) -> np.ndarray:
    fire = None
    for c in rule.any_of:
        ok = np.ones(len(ratios.s.total), dtype=bool)
        for w in c.windows:
            ok &= ratios.get(w) > c.threshold
        fire = ok if fire is None else fire | ok
    assert fire is not None
    return for_filter(fire, rule.for_minutes)


def episodes(firing: np.ndarray, start: int = 0, merge_gap: int = 0) -> list[tuple[int, int]]:
    """(start, end) minute pairs, end exclusive, for runs of True starting at or after `start`.

    Runs separated by fewer than `merge_gap` quiet minutes become one episode.
    """
    f = firing.astype(np.int8)
    d = np.diff(np.concatenate(([0], f, [0])))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    out: list[tuple[int, int]] = []
    for a, b in zip(starts.tolist(), ends.tolist(), strict=True):
        if out and a - out[-1][1] < merge_gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return [(a, b) for a, b in out if a >= start]


@dataclass
class Episode:
    slo_id: str
    strategy: str
    severity: str
    start: int
    end: int
    incident: str | None = None
    true: bool = False


@dataclass
class StrategyEval:
    strategy: str
    severity: str  # "page" or "page+ticket"
    episodes: list[Episode] = field(default_factory=list)
    detections: dict[str, int] = field(default_factory=dict)  # incident id -> delay min
    resets: dict[str, int] = field(default_factory=dict)  # incident id -> min after end


def attribute(eps: list[Episode], incidents: list[Incident]) -> None:
    for e in eps:
        for inc in incidents:
            if inc.start <= e.start <= inc.end + GRACE:
                e.incident = inc.id
                e.true = inc.significant
                break


def evaluate_stream(stream: Stream, strategies: dict[str, Strategy],
                    eval_start: int) -> dict[str, list[Episode]]:
    """All alert episodes for one SLO, keyed by strategy."""
    ratios = RatioCache(stream)
    out: dict[str, list[Episode]] = {}
    for key, st in strategies.items():
        eps = []
        for rule in st.rules:
            fire = rule_firing(rule, ratios)
            eps += [Episode(stream.slo_id, key, rule.severity, a, b)
                    for a, b in episodes(fire, eval_start, MERGE_GAP)]
        attribute(eps, stream.incidents)
        out[key] = sorted(eps, key=lambda e: e.start)
    return out


def score(eps: list[Episode], incidents: list[Incident], days: float) -> dict:
    """Summary metrics for one strategy and severity filter, over many SLOs."""
    sig = [i for i in incidents if i.significant]
    by_inc: dict[str, list[Episode]] = {}
    for e in eps:
        if e.incident:
            by_inc.setdefault(e.incident, []).append(e)
    det, resets = {}, {}
    for inc in sig:
        mine = by_inc.get(inc.id, [])
        if mine:
            det[inc.id] = min(e.start for e in mine) - inc.start
            resets[inc.id] = max(0, max(e.end for e in mine) - inc.end)
    tp = sum(e.true for e in eps)
    minor = sum(1 for e in eps if e.incident and not e.true)
    weeks = days / 7
    return {
        "notifications": len(eps),
        "true": tp,
        "false": len(eps) - tp,
        "false_on_minor_incidents": minor,
        "false_on_noise": len(eps) - tp - minor,
        "precision": round(tp / len(eps), 4) if eps else None,
        "recall": round(len(det) / len(sig), 4) if sig else None,
        "detected": len(det),
        "significant_incidents": len(sig),
        "median_detection_min": _median(det.values()),
        "median_reset_min": _median(resets.values()),
        "per_week": round(len(eps) / weeks, 2),
        "false_per_week": round((len(eps) - tp) / weeks, 2),
        "detections": det,
        "resets": resets,
    }


def _median(vals) -> float | None:
    v = list(vals)
    return round(float(np.median(v)), 1) if v else None


ROWS = [
    ("static", ("page",), "Static error rate threshold"),
    ("single_window", ("page",), "Single-window burn rate (1h, 14.4x)"),
    ("multiwindow", ("page",), "Multiwindow multi-burn-rate: pages"),
    ("multiwindow", ("page", "ticket"), "Multiwindow multi-burn-rate: pages + tickets"),
]


def summarize(all_eps: dict[str, list[Episode]], incidents: list[Incident], days: float) -> dict:
    rows = []
    for key, sevs, label in ROWS:
        eps = [e for e in all_eps[key] if e.severity in sevs]
        s = score(eps, incidents, days)
        rows.append({"strategy": key, "severities": list(sevs), "label": label, **s})
    kinds: dict[str, dict] = {}
    for inc in incidents:
        k = kinds.setdefault(inc.kind, {"kind": inc.kind, "count": 0, "significant": 0,
                                        "median_budget_consumed": [], "by_row": {}})
        k["count"] += 1
        k["significant"] += inc.significant
        k["median_budget_consumed"].append(inc.budget_consumed)
    for k in kinds.values():
        k["median_budget_consumed"] = round(float(np.median(k["median_budget_consumed"])), 4)
        for (key, sevs, _), row in zip(ROWS, rows, strict=True):
            sig = [i for i in incidents if i.kind == k["kind"] and i.significant]
            det = [row["detections"][i.id] for i in sig if i.id in row["detections"]]
            ids = {i.id for i in incidents if i.kind == k["kind"]}
            eps = [e for e in all_eps[key] if e.severity in sevs and e.incident in ids]
            k["by_row"]["+".join([key, *sevs])] = {
                "detected": len(det), "of": len(sig),
                "median_detection_min": _median(det),
                "notifications": len(eps),
            }
    return {"rows": rows, "kinds": sorted(kinds.values(), key=lambda k: k["kind"])}


def evaluate_all(streams: list[Stream], strategies_by_slo: dict[str, dict[str, Strategy]],
                 eval_start: int, days: float) -> dict:
    all_eps: dict[str, list[Episode]] = {k: [] for k in STRATEGIES}
    incidents: list[Incident] = []
    for s in streams:
        per = evaluate_stream(s, strategies_by_slo[s.slo_id], eval_start)
        for k, eps in per.items():
            all_eps[k] += eps
        incidents += s.incidents
    summary = summarize(all_eps, incidents, days)
    return {"summary": summary, "episodes": all_eps, "incidents": incidents}


def episode_dict(e: Episode, warmup: int) -> dict:
    return {"slo_id": e.slo_id, "strategy": e.strategy, "severity": e.severity,
            "start_day": round((e.start - warmup) / DAY, 4),
            "end_day": round((e.end - warmup) / DAY, 4), "minutes": e.end - e.start,
            "incident": e.incident, "true": e.true}
