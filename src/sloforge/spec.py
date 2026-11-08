"""SLO spec: the YAML format, its schema (pydantic), and loading with readable errors.

One file per service. Example:

    apiVersion: sloforge/v1
    service: checkout-api
    owner: team-payments
    tier: critical
    slos:
      - name: availability
        objective: 99.9
        window: 30d
        sli:
          type: availability
          metric: http_requests_total
          labels: {job: checkout-api}
          bad: {code: "5.."}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

NAME_RE = r"^[a-z][a-z0-9-]{1,62}$"
METRIC_RE = r"^[a-zA-Z_:][a-zA-Z0-9_:]*$"
LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}


def minutes(duration: str) -> int:
    """'5m' -> 5, '6h' -> 360, '3d' -> 4320."""
    m = DURATION_RE.match(duration)
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"invalid duration {duration!r}, use a whole number with m, h, or d")
    return int(m.group(1)) * UNIT_MINUTES[m.group(2)]


def _check_duration(v: str) -> str:
    minutes(v)
    return v


def _check_labels(v: dict[str, str]) -> dict[str, str]:
    for k in v:
        if not LABEL_RE.match(k):
            raise ValueError(f"invalid label name {k!r}")
        if k.startswith("__"):
            raise ValueError(f"label name {k!r} is reserved")
    return v


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AvailabilitySLI(Strict):
    """Ratio of good events to total events from one counter. Bad events match `bad`."""

    type: Literal["availability"]
    metric: str = Field(pattern=METRIC_RE)
    labels: dict[str, str] = Field(default_factory=dict)
    bad: dict[str, str] = Field(min_length=1, description="regex matchers that select bad events")

    _labels = field_validator("labels", "bad")(_check_labels)

    @model_validator(mode="after")
    def _no_overlap(self) -> AvailabilitySLI:
        both = set(self.labels) & set(self.bad)
        if both:
            raise ValueError(f"label {sorted(both)[0]!r} is in both labels and bad")
        return self


class LatencySLI(Strict):
    """Share of requests faster than a threshold, from a Prometheus histogram."""

    type: Literal["latency"]
    metric: str = Field(pattern=METRIC_RE, description="histogram base name, without _bucket")
    labels: dict[str, str] = Field(default_factory=dict)
    threshold_seconds: float = Field(gt=0, le=60)

    _labels = field_validator("labels")(_check_labels)

    @field_validator("metric")
    @classmethod
    def _base_name(cls, v: str) -> str:
        for suffix in ("_bucket", "_count", "_sum"):
            if v.endswith(suffix):
                raise ValueError(f"use the histogram base name without {suffix!r}")
        return v


SLI = Annotated[AvailabilitySLI | LatencySLI, Field(discriminator="type")]


class BurnWindow(Strict):
    """One multiwindow condition: burn rate above `burn_rate` over both windows."""

    long: str
    short: str
    burn_rate: float = Field(gt=0)
    severity: Literal["page", "ticket"]

    _durations = field_validator("long", "short")(_check_duration)

    @model_validator(mode="after")
    def _short_shorter(self) -> BurnWindow:
        if minutes(self.short) >= minutes(self.long):
            raise ValueError(f"short window {self.short} must be shorter than long {self.long}")
        return self


class SingleWindow(Strict):
    window: str = "1h"
    burn_rate: float = Field(default=14.4, gt=0)

    _durations = field_validator("window")(_check_duration)


# The SRE workbook's recommended set (chapter 5, "Alerting on SLOs").
WORKBOOK_WINDOWS = [
    BurnWindow(long="1h", short="5m", burn_rate=14.4, severity="page"),
    BurnWindow(long="6h", short="30m", burn_rate=6, severity="page"),
    BurnWindow(long="3d", short="6h", burn_rate=1, severity="ticket"),
]


class Alerting(Strict):
    strategy: Literal["static", "single_window", "multiwindow"] = Field(
        default="multiwindow", description="the strategy `generate` deploys")
    static_threshold: float | None = Field(
        default=None, gt=0, lt=1, description="error ratio over 5m; default 10x the budget"
    )
    static_for: str = "2m"
    single_window: SingleWindow = Field(default_factory=SingleWindow)
    multiwindow: list[BurnWindow] = Field(default_factory=lambda: list(WORKBOOK_WINDOWS),
                                          min_length=1)

    _durations = field_validator("static_for")(_check_duration)


class SLO(Strict):
    name: str = Field(pattern=NAME_RE)
    description: str = ""
    objective: float = Field(gt=50, lt=100, description="percent, for example 99.9")
    window: Literal["28d", "30d"]
    sli: SLI
    alerting: Alerting = Field(default_factory=Alerting)

    @field_validator("objective")
    @classmethod
    def _max_nines(cls, v: float) -> float:
        if v > 99.999:
            raise ValueError("objectives above 99.999 leave under 26 seconds of budget a month")
        return v

    @property
    def budget(self) -> float:
        """Allowed error ratio: 1 - objective."""
        return round(1 - self.objective / 100, 10)

    @property
    def window_minutes(self) -> int:
        return minutes(self.window)

    @property
    def static_threshold(self) -> float:
        a = self.alerting.static_threshold
        return a if a is not None else min(0.5, round(10 * self.budget, 10))

    @model_validator(mode="after")
    def _alerting_feasible(self) -> SLO:
        budget = self.budget
        max_burn = 1 / budget
        rules = [(f"multiwindow[{i}]", w.burn_rate, w.long)
                 for i, w in enumerate(self.alerting.multiwindow)]
        rules.append(("single_window", self.alerting.single_window.burn_rate,
                      self.alerting.single_window.window))
        for where, burn, long in rules:
            if burn > max_burn:
                raise ValueError(
                    f"alerting.{where}: burn rate {burn} can never fire, the highest possible "
                    f"burn rate for objective {self.objective} is {max_burn:g} (100% errors)")
            if minutes(long) > self.window_minutes:
                raise ValueError(f"alerting.{where}: window {long} is longer than the SLO "
                                 f"window {self.window}")
        st = self.alerting.static_threshold
        if st is not None and st <= budget:
            raise ValueError(
                f"alerting.static_threshold {st} is at or below the error budget {budget:g}; "
                f"it would page while the SLO is being met")
        return self


class ServiceSpec(Strict):
    apiVersion: Literal["sloforge/v1"]
    service: str = Field(pattern=NAME_RE)
    owner: str = Field(min_length=1)
    tier: Literal["critical", "standard", "low"] = "standard"
    description: str = ""
    slos: list[SLO] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> ServiceSpec:
        seen: set[str] = set()
        for s in self.slos:
            if s.name in seen:
                raise ValueError(f"duplicate SLO name {s.name!r}")
            seen.add(s.name)
        return self


@dataclass
class SpecError:
    file: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.path}: {self.message}" if self.path else \
            f"{self.file}: {self.message}"


class SpecInvalid(Exception):
    def __init__(self, errors: list[SpecError]) -> None:
        super().__init__("\n".join(map(str, errors)))
        self.errors = errors


def _loc(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif part in ("availability", "latency") and out.endswith("sli"):
            continue  # discriminator tag pydantic adds to the location
        else:
            out += f".{part}" if out else str(part)
    return out


def parse(text: str, file: str = "<string>") -> ServiceSpec:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark else ""
        raise SpecInvalid([SpecError(file, where, f"YAML syntax error: "
                                     f"{getattr(e, 'problem', None) or e}")]) from None
    if not isinstance(data, dict):
        raise SpecInvalid([SpecError(file, "", "expected a mapping at the top level")])
    try:
        return ServiceSpec.model_validate(data)
    except ValidationError as e:
        errs = []
        for err in e.errors():
            msg = err["msg"].removeprefix("Value error, ")
            errs.append(SpecError(file, _loc(err["loc"]), msg))
        raise SpecInvalid(errs) from None


def load_file(path: Path) -> ServiceSpec:
    return parse(path.read_text(), str(path))


def spec_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted([*p.glob("*.yaml"), *p.glob("*.yml")])
        else:
            out.append(p)
    return out


def load_all(paths: list[Path]) -> list[ServiceSpec]:
    """Load every spec; raise one SpecInvalid listing every error in every file."""
    specs, errors = [], []
    files = spec_files(paths)
    if not files:
        raise SpecInvalid([SpecError(", ".join(map(str, paths)), "", "no spec files found")])
    for f in files:
        try:
            specs.append(load_file(f))
        except SpecInvalid as e:
            errors += e.errors
    names: dict[str, str] = {}
    for f, s in zip(files, specs, strict=False):
        if not errors and s.service in names:
            errors.append(SpecError(str(f), "service",
                                    f"service {s.service!r} is also defined in {names[s.service]}"))
        names[s.service] = str(f)
    if errors:
        raise SpecInvalid(errors)
    return specs


def slo_id(spec: ServiceSpec, slo: SLO) -> str:
    return f"{spec.service}/{slo.name}"


def json_schema() -> dict:
    return ServiceSpec.model_json_schema()
