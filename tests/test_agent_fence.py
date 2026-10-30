"""acme-bot tests/test_agent_fence.py — the estate-context fence in
`agent.build_options()`, and the resume fallback that the fence's new cwd
made necessary.

The fence is removal by construction, so these tests assert on the OPTIONS
the SDK is handed, not on anything the model says. The conftest fake
`ClaudeAgentOptions` is `lambda **kwargs: kwargs`, so `build_options()`
returns exactly the keyword arguments the real SDK would receive — which
is the thing under test.

Why each assertion is load-bearing (see agent.py's module docstring):
- `setting_sources == []` emits `--setting-sources=` (empty), so the CLI
  loads no user/project CLAUDE.md, no memory index, no settings hooks.
  Left unset (the SDK default `None`) the flag is omitted and the CLI
  loads all of them.
- `strict_mcp_config is True` emits `--strict-mcp-config`, so only the
  in-process `acme` server loads — not any MCP server configured on the host
  servers or their instruction blurbs.
- `cwd` outside every git repository means the CLI attaches no git status
  ("Git user: ...", branch name) and finds no project CLAUDE.md above it.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import os

import pytest

import agent as agent_module

REPO_ROOT = APP_DIR  # this repository's root


def _options(message: str = "hello", resume=None) -> dict:
    return agent_module.build_options(message, resume)


# --- the three fence options -------------------------------------------------

def test_setting_sources_is_an_explicit_empty_list():
    opts = _options()
    assert "setting_sources" in opts, "setting_sources must be passed explicitly, not left at the SDK default"
    assert opts["setting_sources"] == []
    assert opts["setting_sources"] is not None


def test_strict_mcp_config_is_true():
    assert _options()["strict_mcp_config"] is True


def test_cwd_is_not_inside_this_repository():
    cwd = Path(_options()["cwd"]).resolve()
    assert REPO_ROOT.resolve() not in cwd.parents
    assert cwd != REPO_ROOT.resolve()
    assert APP_DIR.resolve() != cwd


def test_cwd_has_no_git_repository_above_it():
    """The positive control for the assertion above: git itself is asked.
    `rev-parse --show-toplevel` succeeds (rc 0) inside any repo and fails
    (rc != 0) when no repo is above the directory — this same call run
    against the repo root returns 0, proving the check can see a repo."""
    cwd = _options()["cwd"]
    Path(cwd).mkdir(parents=True, exist_ok=True)

    outside = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    assert outside.returncode != 0, f"a git repository is above the SDK cwd: {outside.stdout.strip()}"

    control = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    assert control.returncode == 0, "positive control failed: git cannot see the repo it is standing in"


def test_cwd_is_configurable_via_env_and_is_created(tmp_path, monkeypatch):
    target = tmp_path / "runtime-dir"
    monkeypatch.setenv("ACME_SDK_CWD", str(target))
    assert not target.exists()

    cwd = _options()["cwd"]

    assert cwd == str(target)
    assert target.is_dir(), "the configured cwd must be created on first use"


# --- the tool fence is unchanged by the context fence -----------------------

def test_the_tool_fence_is_untouched():
    opts = _options("book a room")
    assert opts["tools"] == []
    assert opts["allowed_tools"] == list(agent_module.TOOL_NAMES)
    assert len(opts["allowed_tools"]) == 7
    assert "mcp__acme__grant_media_perk" in opts["allowed_tools"]
    assert "mcp__acme__send_receipt" in opts["allowed_tools"]
    assert opts["can_use_tool"] is not None
    assert opts["mcp_servers"].keys() == {"acme"}
    for banned in agent_module._BANNED_ENV:
        assert opts["env"][banned] == ""


def test_engine_runs_on_the_operator_api_key_with_a_credential_free_home(monkeypatch):
    """Never a personal Claude login: HOME is the bot's own empty directory and
    the only credential the CLI gets is the operator's ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    env = _options("hello")["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-test"
    assert env["HOME"].endswith("acme-home") and env["HOME"] != os.path.expanduser("~")
    assert not (Path(env["HOME"]) / ".claude" / ".credentials.json").exists()


# Captured at module import time (collection), BEFORE the autouse `_fake_sdk`
# fixture in conftest.py ever runs — this plain reference to the ORIGINAL
# _load_sdk function is unaffected by monkeypatch.setattr(agent_module,
# "_load_sdk", ...) in any test, so it lets exactly one test call the REAL
# gate closure (built from the real claude_agent_sdk decorators) without
# touching agent.py itself or undoing any other test's patches.
_REAL_LOAD_SDK = agent_module._load_sdk


@pytest.mark.skipif(
    not agent_module.sdk_available(),
    reason="claude_agent_sdk not installed on this interpreter (see agent.py's module docstring)",
)
def test_real_gate_allows_exactly_seven_acme_tools_and_denies_bash():
    """The fence test, extended to seven tool names now that send_receipt
    is the seventh Acme tool: the REAL can_use_tool gate built by
    agent._load_sdk() (not the conftest fake every other test in this
    file relies on) allows exactly the seven mcp__acme__* names and still
    denies 'Bash' by name — the harness fence is untouched by adding a
    seventh tool."""
    assert len(agent_module.TOOL_NAMES) == 7
    assert "mcp__acme__grant_media_perk" in agent_module.TOOL_NAMES
    assert "mcp__acme__send_receipt" in agent_module.TOOL_NAMES

    sdk = _REAL_LOAD_SDK()
    gate = sdk["gate"]

    for name in agent_module.TOOL_NAMES:
        result = asyncio.run(gate(name, {}, None))
        assert type(result).__name__ == "PermissionResultAllow", name

    denied = asyncio.run(gate("Bash", {}, None))
    assert type(denied).__name__ == "PermissionResultDeny"


def test_model_routing_still_works_through_build_options():
    assert _options("hello")["model"] == "claude-haiku-4-5-20251001"
    assert _options("I want to book a room")["model"] == "claude-sonnet-5"


# --- resume fallback --------------------------------------------------------

class _ResumeRefusingClient:
    """Fake SDK client that fails exactly the way a stale `--resume` fails:
    the CLI cannot find the stored session under the new cwd and exits with
    an error, which the SDK surfaces as an exception out of the context
    manager. A fresh session (resume None) works normally."""

    attempts: list = []

    def __init__(self, options=None):
        self._options = options or {}
        _ResumeRefusingClient.attempts.append(self._options.get("resume"))

    async def __aenter__(self):
        if self._options.get("resume"):
            raise RuntimeError("No conversation found with session ID: %s" % self._options["resume"])
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, message):
        self._message = message

    async def receive_response(self):
        from conftest import FakeMessage
        yield FakeMessage(result=f"(fresh reply to: {self._message})", session_id="fresh-sdk-session-9")


def test_failed_resume_falls_back_to_a_fresh_session(_fake_sdk, caplog):
    _ResumeRefusingClient.attempts = []
    _fake_sdk["ClaudeSDKClient"] = _ResumeRefusingClient

    with caplog.at_level("WARNING", logger="acme.agent"):
        result = asyncio.run(agent_module.run_turn("hello again", "stale-session-id-from-the-old-cwd"))

    assert _ResumeRefusingClient.attempts == ["stale-session-id-from-the-old-cwd", None], \
        "the resumed attempt must be tried first, then exactly one fresh retry"
    assert "fresh reply" in result.reply
    assert result.sdk_session_id == "fresh-sdk-session-9"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert sum("stale-session-id-from-the-old-cwd" in w for w in warnings) == 1, \
        "the fallback is logged exactly once"


def test_a_fresh_turn_is_not_retried(_fake_sdk):
    """The fallback must not turn one no-resume failure into two spawns."""
    _ResumeRefusingClient.attempts = []

    class _AlwaysFails(_ResumeRefusingClient):
        async def __aenter__(self):
            raise RuntimeError("spawn failed")

    _fake_sdk["ClaudeSDKClient"] = _AlwaysFails

    with pytest.raises(RuntimeError):
        asyncio.run(agent_module.run_turn("hello", None))

    assert _ResumeRefusingClient.attempts == [None]


def test_chat_route_returns_200_when_a_stored_session_id_cannot_resume(_fake_sdk, monkeypatch):
    """The caller's path, end to end: a session row already holds an
    sdk_session_id from before the cwd moved. Before the fallback this was
    a 502 for the guest on every turn of every pre-existing session."""
    from fastapi.testclient import TestClient

    import app as app_module
    import db
    from conftest import TEST_API_KEY

    _ResumeRefusingClient.attempts = []
    _fake_sdk["ClaudeSDKClient"] = _ResumeRefusingClient

    with TestClient(app_module.app) as client:
        session_id = client.post("/api/session", headers={"X-Api-Key": TEST_API_KEY}).json()["session_id"]
        db.update_session(session_id, sdk_session_id="stale-id-written-before-the-fence", messages=[])

        resp = client.post("/api/chat", headers={"X-Api-Key": TEST_API_KEY},
                           json={"session_id": session_id, "message": "hi"})

    assert resp.status_code == 200, resp.text
    assert "fresh reply" in resp.json()["reply"]
    assert _ResumeRefusingClient.attempts == ["stale-id-written-before-the-fence", None]
