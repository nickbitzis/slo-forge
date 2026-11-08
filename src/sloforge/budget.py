"""Error budget policy and the deploy gate.

The policy is YAML. Each rule has a condition (budget below a level, or a page-severity alert
firing) and the change types it blocks. The gate checks every SLO of a service at one moment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sloforge.spec import SpecError, SpecInvalid, _loc

CHANGE_TYPES = ("feature", "config", "fix", "rollback")
ChangeType = Literal["feature", "config", "fix", "rollback"]


class When(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_remaining_below: float | None = Field(default=None, ge=-1, le=1)
    page_firing: bool | None = None

    @model_validator(mode="after")
    def _one(self) -> When:
        if (self.budget_remaining_below is None) == (self.page_firing is None):
            raise ValueError("set exactly one of budget_remaining_below or page_firing")
        return self


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str = ""
    when: When
    block: list[ChangeType] = Field(min_length=1)
    tiers: list[Literal["critical", "standard", "low"]] | None = None


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiVersion: Literal["sloforge/v1"]
    kind: Literal["ErrorBudgetPolicy"]
    policies: list[PolicyRule] = Field(min_length=1)


def parse_policy(text: str, file: str = "<string>") -> Policy:
    try:
        return Policy.model_validate(yaml.safe_load(text))
    except ValidationError as e:
        raise SpecInvalid([SpecError(file, _loc(err["loc"]),
                                     err["msg"].removeprefix("Value error, "))
                           for err in e.errors()]) from None


def load_policy(path: Path) -> Policy:
    return parse_policy(path.read_text(), str(path))


@dataclass
class SLOState:
    slo_id: str
    budget_remaining: float
    page_firing: bool


def decide(policy: Policy, service: str, tier: str, change: str,
           states: list[SLOState]) -> dict:
    if change not in CHANGE_TYPES:
        raise ValueError(f"change must be one of {', '.join(CHANGE_TYPES)}")
    reasons = []
    for rule in policy.policies:
        if rule.tiers and tier not in rule.tiers:
            continue
        for st in states:
            w = rule.when
            hit = (st.budget_remaining < w.budget_remaining_below
                   if w.budget_remaining_below is not None else st.page_firing == w.page_firing)
            if hit:
                reasons.append({"policy": rule.name, "description": rule.description,
                                "slo_id": st.slo_id, "blocks": list(rule.block),
                                "applies": change in rule.block,
                                "budget_remaining": round(st.budget_remaining, 4)})
    blocking = [r for r in reasons if r["applies"]]
    return {
        "service": service, "change": change, "allowed": not blocking,
        "reasons": blocking, "active_policies": reasons,
        "slos": [{"slo_id": s.slo_id, "budget_remaining": round(s.budget_remaining, 4),
                  "page_firing": s.page_firing} for s in states],
    }
