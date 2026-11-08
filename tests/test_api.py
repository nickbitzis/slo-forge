from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sloforge.api import create_app


@pytest.fixture(scope="module")
def client(engine):
    with TestClient(create_app(engine=engine)) as c:
        yield c


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "slos": 22}


def test_list_slos(client):
    d = client.get("/api/slos").json()
    assert d["day"] == pytest.approx(90, abs=0.01) and len(d["slos"]) == 22
    inv = next(s for s in d["slos"] if s["slo_id"] == "inventory-service/availability")
    assert inv["status"] == "at_risk" and inv["budget_remaining"] < 0.25
    orders = next(s for s in d["slos"] if s["slo_id"] == "orders-service/availability")
    assert orders["status"] == "burning" and orders["burn_rates"]["1h"] > 14.4
    assert set(orders["burn_rates"]) == {"5m", "30m", "1h", "6h", "3d"}


def test_list_slos_in_the_past(client):
    d = client.get("/api/slos?day=41.5").json()
    co = next(s for s in d["slos"] if s["slo_id"] == "checkout-api/availability")
    assert co["budget_remaining"] < 0 and co["status"] in ("burning", "exhausted")


def test_slo_detail(client):
    d = client.get("/api/slos/checkout-api/availability?from_day=41&to_day=42").json()
    assert d["from_day"] == pytest.approx(41) and len(d["t"]) == len(d["budget_series"])
    assert set(d["burn_rate_max"]) == {"5m", "30m", "1h", "6h", "3d"}
    assert any(i["kind"] == "outage" for i in d["incidents"])
    assert {a["strategy"] for a in d["alerts"]} == {"static", "single_window", "multiwindow"}
    assert {t["burn_rate"] for t in d["thresholds"]["1h"]} == {14.4}


def test_slo_detail_errors(client):
    assert client.get("/api/slos/nope/availability").status_code == 404
    assert client.get("/api/slos/checkout-api/availability?from_day=5&to_day=2").status_code == 422


def test_evaluation(client):
    d = client.get("/api/evaluation").json()
    rows = {(r["strategy"], tuple(r["severities"])): r for r in d["rows"]}
    static = rows[("static", ("page",))]
    mw = rows[("multiwindow", ("page", "ticket"))]
    # The headline tradeoffs: static pages on blips and misses slow burns.
    assert mw["precision"] > static["precision"] + 0.2
    assert mw["recall"] > static["recall"]
    assert static["per_week"] > 2 * rows[("multiwindow", ("page",))]["per_week"]
    kinds = {k["kind"]: k for k in d["kinds"]}
    assert kinds["slow_burn"]["by_row"]["multiwindow+page+ticket"]["detected"] == \
        kinds["slow_burn"]["significant"]
    assert kinds["slow_burn"]["by_row"]["static+page"]["detected"] < \
        kinds["slow_burn"]["significant"] / 2
    assert kinds["blip"]["by_row"]["static+page"]["notifications"] > 50
    single = rows[("single_window", ("page",))]
    assert single["median_reset_min"] > 4 * rows[("multiwindow", ("page",))]["median_reset_min"]


def test_timeline(client):
    d = client.get("/api/timeline").json()
    assert set(d["alerts"]) == {"static", "single_window", "multiwindow"}
    assert len(d["incidents"]) > 100


def test_gate_blocks_low_budget(client):
    d = client.get("/api/deploy-gate?service=inventory-service&change=feature").json()
    assert d["allowed"] is False and d["reasons"][0]["policy"] == "freeze-features"
    assert client.get("/api/deploy-gate?service=inventory-service&change=fix").json()["allowed"]


def test_gate_post_and_page_firing(client):
    r = client.post("/api/deploy-gate", json={"service": "orders-service", "change": "config"})
    assert r.status_code == 200
    assert r.json()["allowed"] is False and r.json()["reasons"][0]["policy"] == "active-page"
    ok = client.post("/api/deploy-gate", json={"service": "orders-service", "change": "feature",
                                               "day": 30})
    assert ok.json()["allowed"] is True


def test_gate_errors(client):
    assert client.get("/api/deploy-gate?service=nope").status_code == 404
    assert client.get("/api/deploy-gate?service=cart-service&change=yolo").status_code == 422
    assert client.post("/api/deploy-gate", json={"service": "cart-service",
                                                 "change": "yolo"}).status_code == 422


def test_rules_endpoint(client):
    text = client.get("/api/rules/payments-gateway").text
    assert "sloforge:sli_error:ratio_rate5m" in text and "SLOBurnRatePage" in text
    assert "SLOStaticErrorRate" in client.get("/api/rules/payments-gateway?strategy=static").text
    assert client.get("/api/rules/payments-gateway?strategy=nope").status_code == 422


def test_metrics(client):
    client.get("/api/slos")
    text = client.get("/metrics").text
    assert 'sloforge_http_requests_total{route="/api/slos",code="200"}' in text
    assert 'sloforge_error_budget_remaining{service="inventory-service",slo="availability"}' in text
    assert 'sloforge_burn_rate{service="orders-service",slo="availability",window="1h"}' in text
    assert "sloforge_deploy_gate_decisions_total" in text


def test_ui_served(client):
    html = client.get("/").text
    assert "<title>SLO Forge</title>" in html
    assert client.get("/static/app.js").status_code == 200
    assert "prefers-color-scheme: dark" in client.get("/static/styles.css").text


def test_json_logs(client, capsys):
    import json
    import logging

    from sloforge.logs import JsonFormatter
    rec = logging.LogRecord("sloforge.api", logging.INFO, "", 0, "request", None, None)
    rec.fields = {"path": "/x"}
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "request" and out["path"] == "/x" and out["level"] == "info"
