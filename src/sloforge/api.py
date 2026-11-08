"""FastAPI service: SLO status, burn rates, alert evaluation, the deploy gate, and the web UI.

The engine (simulation plus evaluation, a few seconds) is built once at startup. "Now" is the
last simulated minute; most reads accept `day` (0 to 90) to look at an earlier moment.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sloforge import rules
from sloforge.budget import ChangeType
from sloforge.engine import DEFAULT_POLICY, DEFAULT_SPECS, Engine
from sloforge.logs import log, setup_logging
from sloforge.simulator import SimConfig
from sloforge.strategies import STRATEGIES

WEB = Path(__file__).parent / "web"
logger = logging.getLogger("sloforge.api")


class GateRequest(BaseModel):
    service: str
    change: ChangeType = "feature"
    day: float | None = None


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: dict[tuple[str, int], int] = {}
        self.gate: dict[tuple[str, str], int] = {}
        self.latency_sum = 0.0
        self.latency_count = 0

    def observe(self, route: str, status: int, seconds: float) -> None:
        with self.lock:
            self.requests[(route, status)] = self.requests.get((route, status), 0) + 1
            self.latency_sum += seconds
            self.latency_count += 1

    def render(self, engine: Engine | None) -> str:
        out = ["# HELP sloforge_http_requests_total HTTP requests by route and status.",
               "# TYPE sloforge_http_requests_total counter"]
        with self.lock:
            for (route, status), n in sorted(self.requests.items()):
                out.append(f'sloforge_http_requests_total{{route="{route}",code="{status}"}} {n}')
            out += ["# HELP sloforge_http_request_seconds Request latency.",
                    "# TYPE sloforge_http_request_seconds summary",
                    f"sloforge_http_request_seconds_sum {self.latency_sum:.6f}",
                    f"sloforge_http_request_seconds_count {self.latency_count}",
                    "# HELP sloforge_deploy_gate_decisions_total Deploy gate answers.",
                    "# TYPE sloforge_deploy_gate_decisions_total counter"]
            for (svc, result), n in sorted(self.gate.items()):
                out.append(f'sloforge_deploy_gate_decisions_total{{service="{svc}",'
                           f'result="{result}"}} {n}')
        if engine is not None:
            out += ["# HELP sloforge_error_budget_remaining Budget left at simulated now.",
                    "# TYPE sloforge_error_budget_remaining gauge"]
            slos = engine.list_slos()
            for s in slos:
                out.append(f'sloforge_error_budget_remaining{{service="{s["service"]}",'
                           f'slo="{s["slo"]}"}} {s["budget_remaining"]}')
            out += ["# HELP sloforge_burn_rate Burn rate per window at simulated now.",
                    "# TYPE sloforge_burn_rate gauge"]
            for s in slos:
                for w, v in s["burn_rates"].items():
                    out.append(f'sloforge_burn_rate{{service="{s["service"]}",slo="{s["slo"]}",'
                               f'window="{w}"}} {v}')
            out += ["# HELP sloforge_engine_build_seconds Time to simulate and evaluate.",
                    "# TYPE sloforge_engine_build_seconds gauge",
                    f"sloforge_engine_build_seconds {engine.build_seconds}"]
        return "\n".join(out) + "\n"


def create_app(engine: Engine | None = None, specs: Path | None = None,
               policy: Path | None = None, cfg: SimConfig | None = None) -> FastAPI:
    setup_logging(os.environ.get("SLOFORGE_LOG_LEVEL", "INFO"))
    specs = specs or Path(os.environ.get("SLOFORGE_SPECS", DEFAULT_SPECS))
    policy = policy or Path(os.environ.get("SLOFORGE_POLICY", DEFAULT_POLICY))
    state: dict[str, Engine] = {}
    if engine is not None:
        state["engine"] = engine
    metrics = Metrics()

    def eng() -> Engine:
        if "engine" not in state:
            state["engine"] = Engine.from_paths(specs, policy, cfg)
            log(logger, "engine built", slos=len(state["engine"].slos),
                seconds=state["engine"].build_seconds)
        return state["engine"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        eng()
        yield

    app = FastAPI(title="slo-forge", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        dt = time.perf_counter() - t0
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        metrics.observe(path, response.status_code, dt)
        if path not in ("/healthz", "/metrics") and not path.startswith("/static"):
            log(logger, "request", method=request.method, path=request.url.path,
                status=response.status_code, ms=round(dt * 1000, 1))
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        e = state.get("engine")
        return {"status": "ok" if e else "starting", "slos": len(e.slos) if e else 0}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom() -> str:
        return metrics.render(state.get("engine"))

    @app.get("/api/slos")
    def slos(day: float | None = Query(None, ge=0, le=90)) -> dict:
        e = eng()
        return {"day": e.day(e.minute(day)), "slos": e.list_slos(day)}

    @app.get("/api/slos/{service}/{slo}")
    def slo_detail(service: str, slo: str, from_day: float = Query(0, ge=0, le=90),
                   to_day: float | None = Query(None, ge=0, le=90),
                   points: int = Query(720, ge=50, le=3000)) -> dict:
        e = eng()
        sid = f"{service}/{slo}"
        if sid not in e.slos:
            raise HTTPException(404, f"no SLO {sid}")
        if to_day is not None and to_day <= from_day:
            raise HTTPException(422, "to_day must be after from_day")
        return e.detail(sid, from_day, to_day, points)

    @app.get("/api/evaluation")
    def evaluation() -> dict:
        return eng().evaluation()

    @app.get("/api/timeline")
    def timeline() -> dict:
        return eng().timeline()

    @app.get("/api/rules/{service}", response_class=PlainTextResponse)
    def service_rules(service: str, strategy: str | None = None) -> str:
        e = eng()
        if service not in e.specs:
            raise HTTPException(404, f"no service {service}")
        if strategy is not None and strategy not in STRATEGIES:
            raise HTTPException(422, f"strategy must be one of {', '.join(STRATEGIES)}")
        spec = e.specs[service]
        return rules.dump_yaml(rules.recording_rules(spec)) + "---\n" + rules.dump_yaml(
            rules.alerting_rules(spec, strategy))

    def _gate(service: str, change: str, day: float | None) -> JSONResponse:
        e = eng()
        try:
            out = e.gate(service, change, day)
        except KeyError:
            raise HTTPException(404, f"no service {service}") from None
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        result = "allowed" if out["allowed"] else "blocked"
        with metrics.lock:
            metrics.gate[(service, result)] = metrics.gate.get((service, result), 0) + 1
        log(logger, "deploy gate", service=service, change=change, result=result,
            reasons=[r["policy"] for r in out["reasons"]])
        # 200 either way: the answer is in the body. CI reads `allowed`.
        return JSONResponse(out)

    @app.post("/api/deploy-gate")
    def gate_post(req: GateRequest) -> JSONResponse:
        return _gate(req.service, req.change, req.day)

    @app.get("/api/deploy-gate")
    def gate_get(service: str, change: str = "feature",
                 day: float | None = Query(None, ge=0, le=90)) -> JSONResponse:
        return _gate(service, change, day)

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    return app
