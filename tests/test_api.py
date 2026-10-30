from __future__ import annotations

import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
from conftest import TEST_API_KEY, TEST_DAILY_CAP


@pytest.fixture()
def client():
    with TestClient(app_module.app) as c:
        yield c


def test_healthz_is_open_no_auth(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["db"] == db.guest_count()
    assert "date" in body


def test_admin_reset_requires_api_key(client):
    resp = client.post("/admin/reset")
    assert resp.status_code == 401


def test_admin_reset_with_api_key(client):
    resp = client.post("/admin/reset", headers={"X-Api-Key": TEST_API_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reset"] is True
    assert body["counts"]["guests"] == 30


def test_api_session_401_without_key_or_cookie(client):
    resp = client.post("/api/session")
    assert resp.status_code == 401


def test_api_session_200_with_key(client):
    resp = client.post("/api/session", headers={"X-Api-Key": TEST_API_KEY})
    assert resp.status_code == 200
    assert "session_id" in resp.json()


def test_index_sets_widget_cookie_and_widget_can_open_session(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Acme Resorts" in resp.text
    assert "acme_widget" in resp.cookies

    # The cookie the browser now holds is enough to open a session with no key.
    resp2 = client.post("/api/session")
    assert resp2.status_code == 200
    assert "session_id" in resp2.json()


def test_chat_401_without_key_or_cookie(client):
    resp = client.post("/api/chat", json={"session_id": "whatever", "message": "hi"})
    assert resp.status_code == 401


def test_chat_unknown_session_id_is_auto_created_and_returns_200(client):
    """Supersedes the old 404 contract: the tenant's own REST connection
    test generates its own session_id and posts straight to /api/chat with
    no prior /api/session call. An unknown id is now created on first use
    instead of rejected."""
    resp = client.post("/api/chat", headers={"X-Api-Key": TEST_API_KEY},
                        json={"session_id": "tenant-generated-abc123", "message": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "tenant-generated-abc123"
    assert "reply" in body
    assert db.get_session("tenant-generated-abc123") is not None


def test_chat_session_id_over_128_chars_is_rejected(client):
    resp = client.post("/api/chat", headers={"X-Api-Key": TEST_API_KEY},
                        json={"session_id": "x" * 129, "message": "hi"})
    assert resp.status_code == 400


def test_chat_200_with_key_uses_fake_sdk_and_records_tool_calls(client):
    sess = client.post("/api/session", headers={"X-Api-Key": TEST_API_KEY}).json()
    resp = client.post("/api/chat", headers={"X-Api-Key": TEST_API_KEY},
                        json={"session_id": sess["session_id"], "message": "book a room please"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sess["session_id"]
    assert "canned reply" in body["reply"]
    assert body["tool_calls"] == [{"tool": "check_availability", "args": {
        "resort": "Coastal", "check_in": "2026-10-01", "check_out": "2026-10-03",
    }}]


def test_chat_widget_cookie_path_works(client):
    client.get("/")  # mints the acme_widget cookie on the shared TestClient session
    sess = client.post("/api/session").json()
    resp = client.post("/api/chat", json={"session_id": sess["session_id"], "message": "hello"})
    assert resp.status_code == 200
    assert "reply" in resp.json()


def test_session_open_and_close(client):
    sess = client.post("/api/session", headers={"X-Api-Key": TEST_API_KEY}).json()
    resp = client.delete(f"/api/session/{sess['session_id']}", headers={"X-Api-Key": TEST_API_KEY})
    assert resp.status_code == 200
    assert resp.json()["closed"] is True

    # Second close of the same id: not found.
    resp2 = client.delete(f"/api/session/{sess['session_id']}", headers={"X-Api-Key": TEST_API_KEY})
    assert resp2.status_code == 404


def test_session_close_post_endpoint_closes_and_is_idempotent(client):
    """POST-with-body twin of the DELETE route, for a tenant whose "Close
    Session" form can only send a POST payload. Unlike DELETE, it never
    404s — closing twice, or an id that never existed, both still 200 with
    closed: true."""
    sess = client.post("/api/session", headers={"X-Api-Key": TEST_API_KEY}).json()
    session_id = sess["session_id"]

    resp = client.post("/api/session/close", headers={"X-Api-Key": TEST_API_KEY},
                        json={"session_id": session_id})
    assert resp.status_code == 200
    assert resp.json() == {"session_id": session_id, "closed": True}
    assert db.get_session(session_id) is None

    # Idempotent: closing again (already gone) still succeeds.
    resp2 = client.post("/api/session/close", headers={"X-Api-Key": TEST_API_KEY},
                         json={"session_id": session_id})
    assert resp2.status_code == 200
    assert resp2.json() == {"session_id": session_id, "closed": True}

    # An id that never existed at all: still succeeds.
    resp3 = client.post("/api/session/close", headers={"X-Api-Key": TEST_API_KEY},
                         json={"session_id": "never-existed-either"})
    assert resp3.status_code == 200
    assert resp3.json() == {"session_id": "never-existed-either", "closed": True}


def test_session_close_post_requires_session_id(client):
    resp = client.post("/api/session/close", headers={"X-Api-Key": TEST_API_KEY}, json={})
    assert resp.status_code == 400


def test_session_close_post_401_without_key_or_cookie(client):
    resp = client.post("/api/session/close", json={"session_id": "whatever"})
    assert resp.status_code == 401


def test_index_html_links_the_versioned_widget_js(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "widget.js?v=" in resp.text
    assert "widget.css?v=" in resp.text


def test_index_is_no_cache_so_it_never_serves_stale_asset_urls(client):
    resp = client.get("/")
    assert resp.headers["cache-control"] == "no-cache"


def test_static_asset_gets_far_future_immutable_cache_control(client):
    resp = client.get("/static/widget.js")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control


def test_widget_css_forces_hidden_panel_and_bubble_to_display_none():
    """Playwright-caught regression: `.acme-panel { display: flex }` (needed
    so the panel shows once JS clears `hidden`) silently overrode the
    browser's default `[hidden] { display: none }` UA rule — author CSS
    always wins over UA CSS, regardless of selector specificity. That left
    the panel rendered (and intercepting pointer-events meant for the
    bubble) even while still marked hidden, so a click never reached
    openPanel() and the Sam welcome never showed. This locks in the
    explicit [hidden] override for both elements."""
    css_path = Path(__file__).parent.parent / "static" / "widget.css"
    normalized = re.sub(r"\s+", "", css_path.read_text(encoding="utf-8"))
    for selector in (".acme-panel[hidden]", ".acme-bubble[hidden]"):
        idx = normalized.find(selector)
        assert idx != -1, f"{selector} not found in widget.css"
        rule_end = normalized.find("}", idx)
        assert rule_end != -1
        rule = normalized[idx:rule_end]
        assert "display:none!important" in rule, f"{selector} rule must be display:none !important"


def test_content_version_changes_when_file_content_changes(tmp_path):
    """The Cloudflare-cache fix (app._content_version /
    app._ASSET_VERSIONS) only works if a content change produces a
    different version string — proven here against a temp file, not the
    real static/widget.js, so the test can't accidentally pass just
    because that file hasn't changed."""
    asset = tmp_path / "probe.js"
    asset.write_text("console.log('v1');")
    v1 = app_module._content_version(asset)

    asset.write_text("console.log('v2, different content entirely');")
    v2 = app_module._content_version(asset)

    assert v1 != v2
    assert len(v1) == 10
    assert len(v2) == 10


def test_daily_cap_returns_429_beyond_it(client):
    cap = int(TEST_DAILY_CAP)
    sess = client.post("/api/session", headers={"X-Api-Key": TEST_API_KEY}).json()
    session_id = sess["session_id"]

    statuses = []
    for _ in range(cap + 2):
        resp = client.post("/api/chat", headers={"X-Api-Key": TEST_API_KEY},
                            json={"session_id": session_id, "message": "hi"})
        statuses.append(resp.status_code)

    assert statuses[:cap] == [200] * cap
    assert statuses[cap] == 429
    assert statuses[cap + 1] == 429


def test_inline_assets_embeds_css_and_js_with_no_static_requests(monkeypatch):
    """ACME_INLINE_ASSETS=1 (v2): the page carries its own CSS/JS, so a proxy
    that drops /static/* (a TLS-inspecting corporate proxy) still gets a styled page."""
    import app as app_module
    monkeypatch.setattr(app_module, "_INLINE_ASSETS", True)
    html = app_module._render_index_html()
    assert "/static/site.css" not in html
    assert "/static/widget.css" not in html
    assert "/static/widget.js" not in html
    assert (app_module.STATIC_DIR / "site.css").read_text(encoding="utf-8").strip()[:60] in html
    assert html.count("<style>") >= 2
