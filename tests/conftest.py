from __future__ import annotations

from pathlib import Path

import pytest

from sloforge.engine import Engine
from sloforge.spec import parse

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

BASE = """
apiVersion: sloforge/v1
service: demo-api
owner: team-demo
slos:
  - name: availability
    objective: 99.9
    window: 30d
    sli:
      type: availability
      metric: http_requests_total
      labels: {job: demo-api}
      bad: {code: "5.."}
"""


@pytest.fixture
def demo_spec():
    return parse(BASE)


@pytest.fixture(scope="session")
def engine() -> Engine:
    return Engine.from_paths(EXAMPLES / "slos", EXAMPLES / "policy.yaml")
