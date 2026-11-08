"""Synthetic request streams at 1 minute resolution, with planted incidents as ground truth.

Each service gets a diurnal and weekly traffic curve. Each SLO gets a baseline error ratio
below its budget with hourly wobble, then incidents add an excess error ratio expressed as a
burn rate (excess ratio = burn * budget). A 30 day warm-up runs first, so the rolling budget
window is full when the 90 day evaluation period starts.
"""

from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass, field

import numpy as np

from sloforge.spec import SLO, ServiceSpec, slo_id

DAY = 1440
WARMUP_DAYS = 30
DAYS = 90
SIGNIFICANT = 0.02  # an incident that spends 2% or more of the budget is budget-threatening

# Requests per minute at the daily peak. Unlisted services get 600.
TRAFFIC = {
    "storefront-web": 9000, "checkout-api": 1400, "payments-gateway": 900, "cart-service": 3200,
    "catalog-api": 5200, "search-api": 2600, "inventory-service": 1800, "pricing-service": 2200,
    "orders-service": 700, "shipping-quotes": 450, "notifications": 260, "auth-service": 4100,
    "recommendations": 1600, "reviews-api": 120, "image-proxy": 7000,
}

# Scripted incidents on top of the random ones: the outage, and late budget burns so the deploy
# gate has something to block at the end of the period. (service, slo, kind, day, minutes, burn)
SCRIPTED = [
    ("checkout-api", "availability", "outage", 41.40, 26, None),
    ("search-api", "availability", "outage", 63.10, 12, None),
    ("inventory-service", "availability", "slow_burn", 80.6, 2.6 * DAY, 7.0),
    ("recommendations", "latency", "deploy_spike", 86.6, 720, 40.0),
    ("orders-service", "availability", "deploy_spike", 89.97, 60, 60.0),
]


@dataclass
class Incident:
    id: str
    slo_id: str
    service: str
    kind: str
    start: int  # minute index, absolute (warm-up included)
    end: int  # exclusive
    burn: float  # excess burn rate during the incident
    budget_consumed: float = 0.0
    significant: bool = False

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def to_dict(self, warmup: int) -> dict:
        d = asdict(self)
        d["start_day"] = round((self.start - warmup) / DAY, 4)
        d["end_day"] = round((self.end - warmup) / DAY, 4)
        d["minutes"] = self.minutes
        return d


@dataclass
class Stream:
    slo_id: str
    service: str
    slo: str
    sli_type: str
    objective: float
    budget: float
    window_min: int
    total: np.ndarray
    bad: np.ndarray
    incidents: list[Incident] = field(default_factory=list)


@dataclass
class SimConfig:
    seed: int = 7
    days: int = DAYS
    warmup_days: int = WARMUP_DAYS
    random_incidents: bool = True
    scripted: bool = True

    @property
    def warmup(self) -> int:
        return self.warmup_days * DAY

    @property
    def n(self) -> int:
        return (self.warmup_days + self.days) * DAY


def _rng(seed: int, key: str) -> np.random.Generator:
    return np.random.default_rng([seed, zlib.crc32(key.encode())])


def traffic(service: str, cfg: SimConfig) -> np.ndarray:
    rng = _rng(cfg.seed, "traffic:" + service)
    peak = TRAFFIC.get(service, 600)
    t = np.arange(cfg.n)
    hour = (t % DAY) / 60
    diurnal = 0.18 + 0.82 * (0.5 - 0.5 * np.cos(2 * np.pi * (hour - 4) / 24))
    weekday = (t // DAY) % 7
    weekly = np.where(weekday >= 5, 0.8, 1.0)
    growth = 1 + 0.1 * t / cfg.n
    noise = rng.lognormal(0, 0.08, cfg.n)
    return rng.poisson(peak * diurnal * weekly * growth * noise).astype(np.int64)


def _place(rng, taken: list[tuple[int, int]], length: int, cfg: SimConfig) -> int | None:
    lo, hi = cfg.warmup + DAY, cfg.n - length - 30
    for _ in range(50):
        s = int(rng.integers(lo, hi))
        if all(s + length + 360 < a or s > b + 360 for a, b in taken):
            return s
    return None


def plan_incidents(spec: ServiceSpec, slo: SLO, cfg: SimConfig) -> list[Incident]:
    sid = slo_id(spec, slo)
    rng = _rng(cfg.seed, "incidents:" + sid)
    out: list[Incident] = []
    taken: list[tuple[int, int]] = []

    def add(kind: str, start: int, length: int, burn: float) -> None:
        end = min(start + length, cfg.n)
        out.append(Incident("", sid, spec.service, kind, start, end, burn))
        taken.append((start, start + length))

    if cfg.scripted:
        for svc, name, kind, day, length, burn in SCRIPTED:
            if svc == spec.service and name == slo.name and day < cfg.days:
                add(kind, cfg.warmup + int(day * DAY), int(length), burn or 1 / slo.budget)
    if cfg.random_incidents:
        scale = cfg.days / DAYS
        plan = [("deploy_spike", rng.poisson(2.5 * scale)), ("blip", rng.poisson(5 * scale))]
        if slo.sli.type == "availability" and rng.random() < 0.35:
            plan.append(("slow_burn", 1))
        if slo.sli.type == "latency" and rng.random() < 0.7:
            plan.append(("latency_regression", 1))
        for kind, count in plan:
            for _ in range(int(count)):
                if kind == "deploy_spike":
                    length, burn = int(rng.integers(12, 46)), float(rng.uniform(25, 90))
                elif kind == "blip":
                    length, burn = int(rng.integers(1, 5)), float(rng.uniform(40, 200))
                elif kind == "slow_burn":
                    length, burn = int(rng.uniform(1.5, 3) * DAY), float(rng.uniform(2.5, 5))
                else:
                    length, burn = int(rng.uniform(4, 10) * 60), float(rng.uniform(8, 16))
                burn = min(burn, 1 / slo.budget)
                s = _place(rng, taken, length, cfg)
                if s is not None:
                    add(kind, s, length, burn)
    out.sort(key=lambda i: i.start)
    return out


def simulate_slo(spec: ServiceSpec, slo: SLO, total: np.ndarray, cfg: SimConfig) -> Stream:
    sid = slo_id(spec, slo)
    rng = _rng(cfg.seed, "errors:" + sid)
    budget = slo.budget
    base = budget * rng.uniform(0.15, 0.35)
    hours = cfg.n // 60 + 1
    wobble = np.repeat(rng.lognormal(0, 0.35, hours), 60)[: cfg.n]
    p = np.minimum(base * wobble, 1.0)
    incidents = plan_incidents(spec, slo, cfg)
    excess = np.zeros(cfg.n)
    for inc in incidents:
        if inc.kind == "outage":
            excess[inc.start:inc.end] = 1.0
        else:
            excess[inc.start:inc.end] = inc.burn * budget
    p_all = np.minimum(p + excess, 1.0)
    bad = rng.binomial(total, p_all).astype(np.int64)
    mean_window_total = total[cfg.warmup:].mean() * slo.window_minutes
    for k, inc in enumerate(incidents):
        extra = ((p_all - p)[inc.start:inc.end] * total[inc.start:inc.end]).sum()
        inc.budget_consumed = round(float(extra / (budget * mean_window_total)), 5)
        inc.significant = inc.budget_consumed >= SIGNIFICANT
        inc.id = f"{sid}#{k + 1}"
    return Stream(sid, spec.service, slo.name, slo.sli.type, slo.objective, budget,
                  slo.window_minutes, total, bad, incidents)


def simulate(specs: list[ServiceSpec], cfg: SimConfig | None = None) -> list[Stream]:
    cfg = cfg or SimConfig()
    out = []
    for spec in specs:
        total = traffic(spec.service, cfg)
        out += [simulate_slo(spec, slo, total, cfg) for slo in spec.slos]
    return out
