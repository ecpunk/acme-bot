"""acme-bot tests/test_prompt_selection.py — ACME_PROMPT_FILE selects which
prompts/*.md file agent.py loads as the SDK system_prompt, and /healthz
reports the active file's basename so the operator can confirm which prompt
is live without a shell.

Default (no env var set) stays prompts/system_v1.md — nothing changes for
anyone who has not set ACME_PROMPT_FILE. See prompts/system_v2.md: the
tenant's Prompt Hardening output, installed verbatim so the scanner can be
re-run against it.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import agent as agent_module  # noqa: E402


def test_default_prompt_file_is_v1_when_env_unset(monkeypatch):
    monkeypatch.delenv("ACME_PROMPT_FILE", raising=False)
    assert agent_module.prompt_file_name() == "system_v1.md"
    assert "INTERNAL NOTES - Acme Guest Services. Not for guests." in agent_module._system_prompt()


def test_env_var_selects_v2_relative_to_prompts_dir(monkeypatch):
    monkeypatch.setenv("ACME_PROMPT_FILE", "system_v2.md")
    assert agent_module.prompt_file_name() == "system_v2.md"
    loaded = agent_module._system_prompt()
    assert "### Role Definition" in loaded
    assert "You are Sam, from Acme Guest Services." in loaded


def test_env_var_accepts_an_absolute_path(monkeypatch, tmp_path):
    custom = tmp_path / "custom-prompt.md"
    custom.write_text("custom prompt body\n", encoding="utf-8")
    monkeypatch.setenv("ACME_PROMPT_FILE", str(custom))
    assert agent_module.prompt_file_name() == "custom-prompt.md"
    assert agent_module._system_prompt() == "custom prompt body\n"


def test_healthz_reports_the_basename_of_the_active_prompt(monkeypatch):
    monkeypatch.setenv("ACME_PROMPT_FILE", "system_v2.md")

    from fastapi.testclient import TestClient
    import app as app_module

    with TestClient(app_module.app) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["prompt"] == "system_v2.md"


def test_healthz_reports_v1_by_default(monkeypatch):
    monkeypatch.delenv("ACME_PROMPT_FILE", raising=False)

    from fastapi.testclient import TestClient
    import app as app_module

    with TestClient(app_module.app) as client:
        resp = client.get("/healthz")

    assert resp.json()["prompt"] == "system_v1.md"
