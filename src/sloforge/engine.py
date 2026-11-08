"""Ties specs, simulation, evaluation, and policy together for the API and the CLI."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from sloforge import budget as budgetmod
from sloforge.evaluate import RatioCache, episode_dict, error_ratio, evaluate_all, rule_firing
from sloforge.simulator import DAY, SimConfig, Stream, simulate
from sloforge.spec import ServiceSpec, load_all, minutes, slo_id
from sloforge.strategies import LABELS, all_strategies

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPECS = ROOT / "examples" / "slos"
DEFAULT_POLICY = ROOT / "examples" / "policy.yaml"
DETAIL_WINDOWS = ("5m", "30m", "1h", "6h", "3d")


class Engine:
    def __init__(self, specs: list[ServiceSpec], policy: budgetmod.Policy,
                 cfg: SimConfig | None = None) -> None:
        t0 = time.perf_counter()
        self.cfg = cfg or SimConfig()
        self.specs = {s.service: s for s in specs}
        self.policy = policy
        self.slos = {slo_id(sp, s): (sp, s) for sp in specs for s in sp.slos}
        self.strategies = {sid: all_strategies(s) for sid, (_, s) in self.slos.items()}
        self.streams: dict[str, Stream] = {s.slo_id: s for s in simulate(specs, self.cfg)}
        res = evaluate_all(list(self.streams.values()), self.strategies, self.cfg.warmup,
                           self.cfg.days)
        self.summary = res["summary"]
        self.episodes = res["episodes"]
        self.incidents = res["incidents"]
        self.now = self.cfg.n - 1  # the last simulated minute
        self._budget: dict[str, np.ndarray] = {}
        self._page: dict[str, np.ndarray] = {}
        for sid, st in self.streams.items():
            ratio = error_ratio(st.bad, st.total, st.window_min)
            self._budget[sid] = 1 - ratio / st.budget
            _, slo = self.slos[sid]
            deployed = self.strategies[sid][slo.alerting.strategy]
            ratios = RatioCache(st)
            fire = np.zeros(self.cfg.n, dtype=bool)
            for r in deployed.rules:
                if r.severity == "page":
                    fire |= rule_firing(r, ratios)
            self._page[sid] = fire
        self.build_seconds = round(time.perf_counter() - t0, 2)

    @classmethod
    def from_paths(cls, specs: Path = DEFAULT_SPECS, policy: Path = DEFAULT_POLICY,
                   cfg: SimConfig | None = None) -> Engine:
        return cls(load_all([specs]), budgetmod.load_policy(policy), cfg)

    # time helpers -------------------------------------------------------
    def minute(self, day: float | None) -> int:
        if day is None:
            return self.now
        m = self.cfg.warmup + int(round(day * DAY))
        return max(self.cfg.warmup, min(self.now, m))

    def day(self, minute: int) -> float:
        return round((minute - self.cfg.warmup) / DAY, 4)

    # reads --------------------------------------------------------------
    def budget_at(self, sid: str, minute: int) -> float:
        return float(self._budget[sid][minute])

    def burn_rates_at(self, sid: str, minute: int) -> dict[str, float]:
        st = self.streams[sid]
        out = {}
        for w in DETAIL_WINDOWS:
            m = minutes(w)
            lo = max(0, minute + 1 - m)
            t = st.total[lo:minute + 1].sum()
            b = st.bad[lo:minute + 1].sum()
            out[w] = round(float(b / t / st.budget), 3) if t else 0.0
        return out

    def status(self, sid: str, minute: int) -> str:
        b = self.budget_at(sid, minute)
        if self._page[sid][minute]:
            return "burning"
        if b < 0:
            return "exhausted"
        if b < 0.25:
            return "at_risk"
        return "ok"

    def slo_summary(self, sid: str, minute: int) -> dict:
        spec, slo = self.slos[sid]
        st = self.streams[sid]
        return {
            "slo_id": sid, "service": spec.service, "slo": slo.name, "owner": spec.owner,
            "tier": spec.tier, "sli_type": slo.sli.type, "objective": slo.objective,
            "window": slo.window, "budget": st.budget,
            "deployed_strategy": slo.alerting.strategy,
            "budget_remaining": round(self.budget_at(sid, minute), 4),
            "burn_rates": self.burn_rates_at(sid, minute),
            "page_firing": bool(self._page[sid][minute]),
            "status": self.status(sid, minute),
            "incidents": sum(1 for i in st.incidents if i.start <= minute),
        }

    def list_slos(self, day: float | None = None) -> list[dict]:
        m = self.minute(day)
        return [self.slo_summary(sid, m) for sid in self.slos]

    def detail(self, sid: str, from_day: float = 0, to_day: float | None = None,
               points: int = 720) -> dict:
        st = self.streams[sid]
        a = self.minute(from_day)
        b = self.minute(to_day) + 1
        if b - a < 60:
            a = max(self.cfg.warmup, b - 60)
        step = max(1, (b - a) // points)
        edges = np.arange(a, b, step)
        n_buckets = len(edges)
        ratios = RatioCache(st)
        burn = {}
        for w in DETAIL_WINDOWS:
            r = ratios.get(w)[a:b] / st.budget
            pad = n_buckets * step - len(r)
            r = np.concatenate([r, np.full(pad, np.nan)]) if pad > 0 else r[: n_buckets * step]
            burn[w] = np.round(np.nanmax(r.reshape(n_buckets, step), axis=1), 3).tolist()
        _, slo = self.slos[sid]
        thresholds = {w: [] for w in DETAIL_WINDOWS}
        for st_key, strat in self.strategies[sid].items():
            for rule in strat.rules:
                for c in rule.any_of:
                    for w in c.windows:
                        if w in thresholds:
                            thresholds[w].append({"strategy": st_key, "severity": rule.severity,
                                                  "burn_rate": round(c.threshold / st.budget,
                                                                     3)})
        eps = [episode_dict(e, self.cfg.warmup) for k in self.episodes
               for e in self.episodes[k] if e.slo_id == sid and e.end > a and e.start < b]
        return {
            **self.slo_summary(sid, b - 1),
            "from_day": self.day(a), "to_day": self.day(b - 1), "step_minutes": int(step),
            "t": [self.day(int(x)) for x in edges],
            "budget_series": np.round(self._budget[sid][edges].astype(float), 4).tolist(),
            "burn_rate_max": burn, "thresholds": thresholds,
            "incidents": [i.to_dict(self.cfg.warmup) for i in st.incidents
                          if i.end > a and i.start < b],
            "alerts": eps,
            "strategy_labels": LABELS,
        }

    def gate(self, service: str, change: str, day: float | None = None) -> dict:
        if service not in self.specs:
            raise KeyError(service)
        m = self.minute(day)
        spec = self.specs[service]
        states = [budgetmod.SLOState(slo_id(spec, s), self.budget_at(slo_id(spec, s), m),
                                     bool(self._page[slo_id(spec, s)][m])) for s in spec.slos]
        out = budgetmod.decide(self.policy, service, spec.tier, change, states)
        out["day"] = self.day(m)
        return out

    def evaluation(self) -> dict:
        rows = [{k: v for k, v in r.items() if k not in ("detections", "resets")}
                for r in self.summary["rows"]]
        return {"days": self.cfg.days, "seed": self.cfg.seed, "slos": len(self.slos),
                "services": len(self.specs), "rows": rows, "kinds": self.summary["kinds"],
                "incidents": [i.to_dict(self.cfg.warmup) for i in self.incidents]}

    def timeline(self) -> dict:
        return {"days": self.cfg.days,
                "incidents": [i.to_dict(self.cfg.warmup) for i in self.incidents],
                "alerts": {k: [episode_dict(e, self.cfg.warmup) for e in v]
                           for k, v in self.episodes.items()},
                "strategy_labels": LABELS}
