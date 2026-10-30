"""acme-bot app.py — FastAPI front door for the Acme Resorts demo target.

Routes: `GET /` (blank Acme page + widget, mints the widget cookie),
`GET /static/*`, `POST /api/session`, `POST /api/chat`,
`DELETE /api/session/{id}`, `POST /api/session/close`, `GET /healthz`,
`POST /admin/reset`.

Auth: `X-Api-Key` (constant-time compare against `ACME_API_KEY`) gates
every `/api/*` and `/admin/*` route. The widget instead gets a per-page-load
HMAC-signed cookie (`acme_widget`, 2h TTL, a random per-process secret) so
the browser needs no key; that cookie is accepted ONLY on
`POST /api/session`, `POST /api/chat`, `DELETE /api/session/{id}`, and
`POST /api/session/close` — an API key works on those four too.
`/admin/*` never accepts the cookie. `/healthz` is open.

`POST /api/chat` no longer 404s on an unknown `session_id`: a tenant's
REST connection test generates its own id and posts straight here with no
prior `POST /api/session` call, so an unknown id (any non-empty string up
to 128 chars) is created on first use instead — `POST /api/session`'s
generated 32-hex ids still work exactly as before. `POST
/api/session/close` is a POST-with-body twin of the DELETE route for a
tenant whose "Close Session" form can only send a body, and is idempotent
on an unknown id (always `closed: true`, never a 404).

Cost guards (behavioural guards are deliberately absent — see tools.py):
an `asyncio.Semaphore(ACME_MAX_CONCURRENT)` caps concurrent model turns, a
SQLite daily counter caps total `/api/chat` calls per UTC day
(`ACME_MAX_REQUESTS_PER_DAY`, 429 beyond it), and each turn is wrapped in a
60s timeout (`agent.TURN_TIMEOUT_SECONDS` — the CLI spawn is slow; a
shorter window was tried and cut off real sessions).

Static asset caching: `GET /` renders `widget.css`/`widget.js` links with
a `?v=<sha1 prefix>` content-hash query string, computed at startup
(`_ASSET_VERSIONS`), and is itself served `Cache-Control: no-cache` so the
HTML naming those URLs is never stale; `/static/*` (`_VersionedStaticFiles`)
takes a far-future `immutable` Cache-Control, safe only because a changed
file gets a new URL — no Cloudflare purge needed on deploy.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import agent
import db
import tools

log = logging.getLogger("acme.app")
logging.basicConfig(level=os.environ.get("ACME_LOG_LEVEL", "INFO"))

APP_DIR = Path(__file__).parent.resolve()
STATIC_DIR = APP_DIR / "static"


def _load_dotenv() -> None:
    """Minimal stdlib .env loader — no python-dotenv dependency for a
    handful of KEY=VALUE lines. Only fills vars not already set (systemd's
    EnvironmentFile, or a real env var, always wins over this file)."""
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


_load_dotenv()

ACME_API_KEY = os.environ.get("ACME_API_KEY", "")
ACME_MAX_CONCURRENT = int(os.environ.get("ACME_MAX_CONCURRENT", "4"))
ACME_MAX_REQUESTS_PER_DAY = int(os.environ.get("ACME_MAX_REQUESTS_PER_DAY", "4000"))

_WIDGET_SECRET = secrets.token_bytes(32)  # per-process; a restart invalidates outstanding widget cookies
_WIDGET_COOKIE = "acme_widget"
_WIDGET_TTL_SECONDS = 2 * 60 * 60

_SEM = asyncio.Semaphore(ACME_MAX_CONCURRENT)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.ensure_initialized()
    log.info("acme-bot: db ready (%d guests), max_concurrent=%d, daily_cap=%d",
              db.guest_count(), ACME_MAX_CONCURRENT, ACME_MAX_REQUESTS_PER_DAY)
    yield


app = FastAPI(title="acme-bot", lifespan=_lifespan)


# --- static asset cache-busting ---------------------------------------------
# Cloudflare (and browsers) cached /static/widget.js under its old URL after
# a v1.1 deploy — cf-cache-status HIT, ~1600s old — so users saw stale JS with
# no purge run. Fix: each versioned asset's URL carries a content-hash query
# string, computed at startup, so a changed file is a NEW url and never a
# stale hit under the old one; /static/* can then take a far-future
# immutable Cache-Control safely, and "/" itself stays no-cache so the HTML
# (which names the current asset URLs) is never served stale.

_VERSIONED_ASSETS = ("widget.css", "widget.js", "site.css")


def _content_version(path: Path) -> str:
    """First 10 hex chars of sha1(file bytes) — the cache-busting version
    for one static asset."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


_ASSET_VERSIONS: dict[str, str] = {
    name: _content_version(STATIC_DIR / name)
    for name in _VERSIONED_ASSETS
    if (STATIC_DIR / name).exists()
}


# ACME_INLINE_ASSETS=1 (set for v2) embeds site.css, widget.css and widget.js
# in the page itself: a laptop behind a TLS-inspecting corporate proxy loads "/" but drops /static/site.css,
# so the page renders unstyled there. Inlined, there is no second request to drop.
_INLINE_ASSETS = os.environ.get("ACME_INLINE_ASSETS", "") == "1"


def _inline_assets(html: str) -> str:
    for name in ("widget.css", "site.css"):
        css = (STATIC_DIR / name).read_text(encoding="utf-8")
        html = html.replace(f'<link rel="stylesheet" href="/static/{name}">', f"<style>\n{css}\n</style>")
    js = (STATIC_DIR / "widget.js").read_text(encoding="utf-8")
    return html.replace('<script src="/static/widget.js"></script>', f"<script>\n{js}\n</script>")


def _render_index_html() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if _INLINE_ASSETS:
        return _inline_assets(html)
    for name, version in _ASSET_VERSIONS.items():
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    return html


class _VersionedStaticFiles(StaticFiles):
    """Every response gets a far-future immutable Cache-Control — safe
    only because a content change produces a new `?v=` URL (see
    _render_index_html), never a stale hit under the old one."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# --- widget cookie -----------------------------------------------------------

def _sign_widget_cookie() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(_WIDGET_SECRET, ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _verify_widget_cookie(value: Optional[str]) -> bool:
    if not value or "." not in value:
        return False
    ts_str, _, sig = value.partition(".")
    expected = hmac.new(_WIDGET_SECRET, ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    return (time.time() - ts) < _WIDGET_TTL_SECONDS


# --- auth ---------------------------------------------------------------

def _valid_api_key(x_api_key: Optional[str]) -> bool:
    return bool(ACME_API_KEY) and bool(x_api_key) and hmac.compare_digest(x_api_key, ACME_API_KEY)


def _require_api_key(x_api_key: Optional[str]) -> None:
    if not _valid_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="missing or invalid X-Api-Key")


def _require_widget_or_api_key(request: Request, x_api_key: Optional[str]) -> None:
    if _valid_api_key(x_api_key):
        return
    if _verify_widget_cookie(request.cookies.get(_WIDGET_COOKIE)):
        return
    raise HTTPException(status_code=401, detail="missing or invalid credentials")


# --- health / admin -------------------------------------------------------

@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "db": db.guest_count(),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "prompt": agent.prompt_file_name(),
        "tools": [n.split("__")[-1] for n in agent.active_tool_names()],
        "minimize": tools.MINIMIZE,
    })


@app.post("/admin/reset")
async def admin_reset(x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    _require_api_key(x_api_key)
    counts = db.reset()
    log.info("acme-bot: admin reset -> %s", counts)
    return JSONResponse({"reset": True, "counts": counts})


# --- session + chat --------------------------------------------------------

@app.post("/api/session")
async def open_session(request: Request, x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    _require_widget_or_api_key(request, x_api_key)
    session_id = uuid.uuid4().hex
    db.create_session(session_id)
    return JSONResponse({"session_id": session_id})


@app.delete("/api/session/{session_id}")
async def close_session(session_id: str, request: Request,
                         x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    _require_widget_or_api_key(request, x_api_key)
    closed = db.delete_session(session_id)
    if not closed:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return JSONResponse({"session_id": session_id, "closed": True})


@app.post("/api/session/close")
async def close_session_post(request: Request, x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """POST-with-body twin of `DELETE /api/session/{id}`, for a tenant
    whose "Close Session" form can only send a POST payload
    (`{"session_id": "..."}`), not a DELETE with a path segment. Unlike
    the DELETE route, this is idempotent on an unknown id — always
    `{"closed": true}`, never a 404 — because the caller's goal (the
    session is gone) is already true either way."""
    _require_widget_or_api_key(request, x_api_key)
    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    db.delete_session(session_id)
    return JSONResponse({"session_id": session_id, "closed": True})


MAX_SESSION_ID_LEN = 128


@app.post("/api/chat")
async def chat(request: Request, x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    _require_widget_or_api_key(request, x_api_key)
    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    message = (body.get("message") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if len(session_id) > MAX_SESSION_ID_LEN:
        raise HTTPException(status_code=400, detail=f"session_id must be {MAX_SESSION_ID_LEN} characters or fewer")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # A tenant's own REST connection test may generate its own session_id
    # and POST straight to /api/chat with no prior /api/session call — an
    # unknown id here used to 404; now it's created on first use so the
    # tenant's test (and any caller that skips Open Session) just works.
    session = db.get_session(session_id)
    if session is None:
        session = db.ensure_session(session_id)

    current = db.get_daily_counter()
    if current >= ACME_MAX_REQUESTS_PER_DAY:
        raise HTTPException(status_code=429, detail="daily request cap reached")
    db.increment_daily_counter()

    import json as _json
    messages = _json.loads(session["messages"]) if session["messages"] else []
    resume = session["sdk_session_id"]

    async with _SEM:
        try:
            result = await asyncio.wait_for(agent.run_turn(message, resume), timeout=agent.TURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="model turn timed out")
        except Exception as exc:  # noqa: BLE001
            log.warning("acme-bot: agent error: %s", exc)
            raise HTTPException(status_code=502, detail=f"agent error: {exc}")

    messages.append({"role": "user", "content": message})
    messages.append({"role": "assistant", "content": result.reply, "tool_calls": result.tool_calls})
    db.update_session(session_id, sdk_session_id=result.sdk_session_id, messages=messages)

    return JSONResponse({
        "session_id": session_id,
        "reply": result.reply,
        "tool_calls": result.tool_calls,
    })


# --- widget page + static -------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> Response:
    resp = HTMLResponse(_render_index_html())
    resp.headers["Cache-Control"] = "no-cache"
    resp.set_cookie(
        _WIDGET_COOKIE,
        _sign_widget_cookie(),
        max_age=_WIDGET_TTL_SECONDS,
        httponly=False,  # the widget's own fetch() relies on the browser sending it automatically; no JS read needed
        samesite="lax",
    )
    return resp


app.mount("/static", _VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("ACME_HOST", "127.0.0.1"), port=int(os.environ.get("ACME_PORT", "8083")))
