"""Grant Radar — FastAPI app.

- Web UI (HTMX + Jinja2) with JWT cookie auth
- REST /ono + /api with X-API-Key
- MCP mounted at /mcp, reachable either with X-API-Key header or via
  capability URL /mcp/k/{key} (pattern Contrarian): the middleware validates
  the key, rewrites the path, and the MCP app stays auth-unaware.
- In-app discovery: nightly link monitor + monthly LLM scan (APScheduler).
"""
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import os

from .auth import AUTH_MODE, bootstrap_admin, check_api_key, gateway_mode
from .db import init_db
from .discovery.link_monitor import run_link_monitor
from .discovery.scanner import run_scan
from .mcp_server import build_asgi_app, mcp
from .routers import api, ui

scheduler = BackgroundScheduler(timezone="Europe/Zurich")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_admin()
    if os.environ.get("GR_SCHEDULER", "1") == "1":
        scheduler.add_job(run_link_monitor, CronTrigger(hour=3, minute=0), id="link_monitor")
        # Scansione semantica mensile: primo del mese, 04:00.
        scheduler.add_job(run_scan, CronTrigger(day=1, hour=4, minute=0), id="llm_scan")
        scheduler.start()
    async with mcp.session_manager.run():
        yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


# Costruita a import time: registra anche il session manager su `mcp`,
# che il lifespan del parent avvia (i mount FastAPI non propagano i lifespan).
mcp_asgi = build_asgi_app()

app = FastAPI(title="Grant Radar", lifespan=lifespan)

# The surfaces that talk to programs rather than to people. There a refusal is a
# status and a JSON body — a rendered page would reach an MCP client or a script
# as a mouthful of markup where it wanted a code.
MACHINE_PREFIXES = ("/api", "/ono", "/mcp")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    """In gateway mode a page that needs an identity fails closed: 503, not 401.

    Not a redirect to /login either: this app switches that route off in gateway
    mode and sends it back to /app, so the two would bounce forever. Production
    never shows it — the gate intercepts first — but a wrong reverse-proxy
    matcher would spin instead of erroring, and a spin is far harder to diagnose
    than a status code. A request arriving here with no identity means the gate
    did not run in front of it, which is a configuration fault and not something
    the reader did wrong, so the answer is a 503 addressed to the operator.

    Machine surfaces keep the 401: they authenticate with their own key, the
    gate is not in front of them, and a JSON status is what their callers read.
    Everything that is not this one case goes to FastAPI's own handler.
    """
    if (exc.status_code == 401 and gateway_mode()
            and not request.url.path.startswith(MACHINE_PREFIXES)):
        return ui.templates.TemplateResponse(
            request,
            "error.html",
            {
                "code": 503,
                "detail": (
                    "This request arrived without an identity from the sign-in "
                    "gate, which means the gate did not run in front of it. "
                    "Check that the reverse-proxy block for this host imports "
                    "the gate's config, and that BORANT_TRUSTED_PROXY matches "
                    "the address requests actually come from — under Docker "
                    "that is a bridge gateway and not 127.0.0.1. The app's log "
                    "names the address it saw."
                ),
                "gateway": True,
                "user": None,
                "pending_count": 0,
            },
            status_code=503,
        )
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    """Gate /mcp. Two ways in: X-API-Key header, or capability URL /mcp/k/{key}."""
    path = request.url.path
    if path == "/mcp" or path.startswith("/mcp/"):
        if path.startswith("/mcp/k/"):
            rest = path[len("/mcp/k/"):]
            key, _, tail = rest.partition("/")
            if not check_api_key(key):
                return JSONResponse({"error": "invalid key"}, status_code=401)
            # Riscrivi il path: l'app MCP non sa nulla dell'autenticazione.
            request.scope["path"] = f"/mcp/{tail}"
        elif not check_api_key(request.headers.get("x-api-key", "")):
            return JSONResponse({"error": "missing or invalid X-API-Key"}, status_code=401)
        # Senza slash finale il mount risponderebbe 307, che i client MCP non seguono.
        if request.scope["path"] == "/mcp":
            request.scope["path"] = "/mcp/"
    return await call_next(request)


@app.get("/healthz")
def healthz():
    """Liveness, and it stays outside any gate.

    Not monitoring: it says the process answers, not that the nightly link
    monitor ran or that the monthly scan did anything. Those live in scan_log.
    """
    return {"ok": True, "mode": AUTH_MODE}


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
app.include_router(api.router)
app.include_router(ui.router)
app.mount("/mcp", mcp_asgi)
