"""acme-bot tests/conftest.py — sys.path setup, tmp DB, blocked network,
mocked SDK client, fixed API key.

No test in this suite touches the real Claude Agent SDK or the network:
`urllib.request.urlopen` is monkeypatched to raise, and `agent._load_sdk`
(the ONE place agent.py imports `claude_agent_sdk`, done lazily so this
suite also runs on an interpreter without the package installed — see
agent.py's module docstring) is monkeypatched to return a fake SDK whose
`ClaudeSDKClient` yields a canned reply and a canned tool call. This also
means the suite passes identically whether or not `claude_agent_sdk` is
actually importable on the running interpreter.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_DIR = Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

TEST_API_KEY = "test-api-key-0123456789abcdef"
TEST_DAILY_CAP = "3"

# app.py reads ACME_API_KEY / ACME_MAX_CONCURRENT / ACME_MAX_REQUESTS_PER_DAY
# into MODULE-LEVEL constants at import time (once). These must be set
# before `import app` happens anywhere in the test session, so they are set
# here at conftest module scope — pytest always loads conftest.py before it
# imports any test module. db.py, by contrast, reads ACME_DB at CALL time
# (never cached), so that one is safely set per-test via monkeypatch below.
os.environ.setdefault("ACME_API_KEY", TEST_API_KEY)
os.environ.setdefault("ACME_MAX_REQUESTS_PER_DAY", TEST_DAILY_CAP)
os.environ.setdefault("ACME_MAX_CONCURRENT", "4")
os.environ.setdefault("ACME_DB", str(Path(tempfile.gettempdir()) / "acme-bot-conftest-default.db"))
# The live .env turns inline assets on, and app.py's .env loader fills any
# var still unset, so pin the default (linked assets) here. The inline-mode
# test flips app._INLINE_ASSETS itself.
os.environ.setdefault("ACME_INLINE_ASSETS", "0")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "acme-test.db"
    monkeypatch.setenv("ACME_DB", str(db_path))
    yield db_path


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("network access is blocked in tests (urllib.request.urlopen called)")
    monkeypatch.setattr("urllib.request.urlopen", _blocked)


class ToolUseBlock:
    """Named to match `type(block).__name__ == "ToolUseBlock"`, the string
    check agent.run_turn() uses to spot a tool call in the message stream
    (the same pattern other in-process SDK hosts use against the real SDK's
    block classes) — this fake only needs the same class NAME, not to be
    the real SDK type."""

    def __init__(self, name: str, input: dict):  # noqa: A002
        self.name = name
        self.input = input


class FakeMessage:
    def __init__(self, content=None, result=None, session_id=None):
        self.content = content
        self.result = result
        self.session_id = session_id


class FakeClaudeSDKClient:
    """Fakes the async-context-manager + query()/receive_response() shape
    ClaudeSDKClient exposes (see agent.run_turn). Yields one ToolUseBlock
    for check_availability, then a final result message with a canned
    reply, so tests can assert on both the reply text and the tool_calls
    list without ever spawning a real `claude` process."""

    last_options = None

    def __init__(self, options=None):
        FakeClaudeSDKClient.last_options = options
        self._options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, message: str) -> None:
        self._message = message

    async def receive_response(self):
        yield FakeMessage(content=[
            ToolUseBlock("mcp__acme__check_availability", {"resort": "Coastal", "check_in": "2026-10-01", "check_out": "2026-10-03"}),
        ])
        yield FakeMessage(result=f"(canned reply to: {self._message})", session_id="fake-sdk-session-1")


def _fake_options(**kwargs):
    return kwargs


@pytest.fixture(autouse=True)
def _sdk_home(monkeypatch, tmp_path):
    """Point the CLI's HOME at a throwaway directory in every test."""
    monkeypatch.setenv("ACME_SDK_HOME", str(tmp_path / "acme-home"))


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    import agent as agent_module

    fake_sdk = {
        "ClaudeAgentOptions": _fake_options,
        "ClaudeSDKClient": FakeClaudeSDKClient,
        "server": object(),
        "gate": object(),
    }
    monkeypatch.setattr(agent_module, "_load_sdk", lambda: fake_sdk)
    yield fake_sdk
