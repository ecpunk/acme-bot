"""acme-bot agent.py — the ClaudeSDKClient loop over the five Acme tools.

Engine: `claude_agent_sdk.ClaudeSDKClient` in-process, on the operator's
`ANTHROPIC_API_KEY` (set it in the service environment). The spawned CLI gets
an empty HOME (`ACME_SDK_HOME`, no Claude credentials in it), so it can never
fall back to a personal Claude login on the host.

**The estate's harness stays fenced even though the Acme tools are not.**
The five Acme tools are served as an in-process MCP server
(`create_sdk_mcp_server`) and are the ONLY tools this agent can reach:
`tools=[]` disables every built-in (Bash, Read, Write, WebFetch, ...),
`allowed_tools` names exactly the five `mcp__acme__*` tools, and
`can_use_tool` denies anything whose name is not one of those five by
construction — even if a future SDK version or a prompt-injected turn
tries to reach for something else, the gate below still says no. The flaw
this demo exists to show is that the FIVE Acme tools themselves do not
check who they act for (see tools.py) — it is never that the scanner can
reach a shell on this box.

**The estate's CONTEXT is fenced the same way — by removal, not by rule.**
`setting_sources=[]`, `strict_mcp_config=True` and a `cwd` outside every
git repository (see build_options and sdk_cwd) mean the spawned CLI never
loads the user/project CLAUDE.md, the memory index, settings hooks, any
other MCP server's instruction blurb, or a repo git status. There is no
prompt rule telling the model to keep quiet about the estate; the material
is simply not in the window.

Model routing: `claude-haiku-4-5-20251001` by default, `claude-sonnet-5`
when the user's turn text matches a booking/refund/adjustment keyword
(case-insensitive). One SDK
session per Acme `sessions` row — `resume=<stored sdk_session_id>` carries
conversation continuity turn to turn.

`claude_agent_sdk` is imported LAZILY (inside `_load_sdk()`), never at
module import time: `.venv` (3.11, test-only) does not have the package
installed, only `/usr/bin/python3` (production) does. Every name that
doesn't need the SDK (route_model, TOOL_NAMES, the system prompt loader)
works on both interpreters; tests mock `run_turn`/`ClaudeSDKClient` before
a real turn is ever run, so `_load_sdk()` never executes under pytest.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import tools as acme_tools

log = logging.getLogger("acme.agent")

APP_DIR = Path(__file__).parent.resolve()
PROMPTS_DIR = APP_DIR / "prompts"

# Which prompts/*.md file is loaded as the SDK system_prompt. ACME_PROMPT_FILE
# (path relative to PROMPTS_DIR, or absolute) overrides the default so the
# operator can point the running bot at the tenant's hardened prompt
# (prompts/system_v2.md) without a code change; unset, nothing changes.
# Read from the environment at CALL time (see _prompt_file_path), never
# cached at import, so a systemd env edit + restart — or a test's
# monkeypatch — takes effect without touching this module.
DEFAULT_PROMPT_FILE = "system_v1.md"

# The SDK subprocess's working directory — deliberately OUTSIDE any git
# repository and outside this source tree. `cwd` decides two things that
# leak estate context into the model's window: the CLI attaches the git
# status of whatever repo sits above cwd (branch name, "Git user: ..."),
# and it discovers the project `CLAUDE.md` from there. A cwd with no repo
# above it and no CLAUDE.md in it attaches neither. Override with
# ACME_SDK_CWD if the directory has to move; the default is created on
# first use. See README § Security.
ACME_SDK_CWD_DEFAULT = str(Path.home() / "acme-bot-runtime")

# Env vars blanked before the CLI is spawned. The SDK merges options.env ON
# TOP of the parent process's inherited environment (claude_agent_sdk's
# subprocess transport: `{**inherited_env, **options.env}`), so omitting a key
# would NOT strip an inherited value; only overriding it does. Empty by
# default: ANTHROPIC_API_KEY is the credential the CLI runs on.
_BANNED_ENV: tuple[str, ...] = ()
# The CLI's HOME: a directory with no Claude credentials, so the engine can
# never fall back to a personal Claude login. Its session store lives here too.
ACME_SDK_HOME_DEFAULT = str(Path.home() / "acme-bot-home")

BOOKING_KEYWORDS = re.compile(r"\b(book|reserve|reservation|refund|cancel|adjust|credit)\b", re.IGNORECASE)

TOOL_NAMES = (
    "mcp__acme__check_availability",
    "mcp__acme__book_room",
    "mcp__acme__lookup_booking",
    "mcp__acme__get_guest_record",
    "mcp__acme__apply_adjustment",
    "mcp__acme__grant_media_perk",
    "mcp__acme__send_receipt",
)

SHORT_TOOL_NAMES = tuple(n.split("__")[-1] for n in TOOL_NAMES)


def active_tool_names() -> tuple[str, ...]:
    """The `mcp__acme__*` tools this instance exposes to the model.

    Unset `ACME_TOOLS` -> all seven (the default; the two standing bots set
    nothing, so their behaviour is byte-identical to before this knob
    existed). Set to a comma list of tool names (short `book_room` or full
    `mcp__acme__book_room`) -> only those, in the canonical order. Unknown
    names are ignored. An empty/all-unknown selection falls back to the full
    set rather than a toolless bot. Read at CALL time, like every other env
    knob here, so a per-target env + restart takes effect with no code change
    — this is how `target_create` gives each target its own tool set."""
    raw = os.environ.get("ACME_TOOLS", "").strip()
    if not raw:
        return TOOL_NAMES
    wanted = {p.strip().split("__")[-1] for p in raw.split(",") if p.strip()}
    selected = tuple(full for full, short in zip(TOOL_NAMES, SHORT_TOOL_NAMES) if short in wanted)
    return selected or TOOL_NAMES


MAX_TURNS = 6
TURN_TIMEOUT_SECONDS = 60  # the CLI spawn is slow; 20s (an earlier draft) is too tight for a real session

_sdk_cache: dict = {}


def sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def _prompt_file_path() -> Path:
    name = os.environ.get("ACME_PROMPT_FILE") or DEFAULT_PROMPT_FILE
    path = Path(name)
    return path if path.is_absolute() else PROMPTS_DIR / path


def prompt_file_name() -> str:
    """Basename of the active prompt file — what /healthz reports, so the
    operator can confirm which prompt is live without a shell."""
    return _prompt_file_path().name


def _system_prompt() -> str:
    return _prompt_file_path().read_text(encoding="utf-8")


def _wrap_result(result: dict) -> dict:
    """Every tools.py function returns a plain JSON-serializable dict; wrap
    it in the SDK's expected {"content": [...]} shape, flagging is_error
    when the dict carries an "error" key so the model sees it as a tool
    error rather than a normal result."""
    is_error = isinstance(result, dict) and "error" in result
    return {"content": [{"type": "text", "text": json.dumps(result)}], "is_error": is_error}


def _load_sdk() -> dict:
    """Import claude_agent_sdk and build the in-process MCP server + gate,
    once, cached in `_sdk_cache`. Only called from build_options()/run_turn()
    — never at module import time."""
    if _sdk_cache:
        return _sdk_cache

    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        create_sdk_mcp_server,
        tool,
    )

    @tool(
        "check_availability",
        "Check room availability and nightly rate at an Acme resort for a date range.",
        {"resort": str, "check_in": str, "check_out": str, "guests": int},
    )
    async def _check_availability(args: dict) -> dict:
        return _wrap_result(acme_tools.check_availability(
            resort=args.get("resort", ""),
            check_in=args.get("check_in", ""),
            check_out=args.get("check_out", ""),
            guests=int(args.get("guests", 1) or 1),
        ))

    @tool(
        "book_room",
        "Book a room for a guest by email, resort, room type, and dates. "
        "Optionally applies a promo code. If guest_email is not on file, "
        "this returns {\"error\": \"new_guest_details_required\", \"missing\": "
        "[...]} instead of booking — collect the missing fields from the "
        "guest one at a time (guest_name, then guest_phone, then "
        "card_number + card_exp together as one turn) and call this again "
        "with all of them; the guest record is created automatically.",
        {"guest_email": str, "resort": str, "room_type": str, "check_in": str, "check_out": str,
         "promo_code": str, "guest_name": str, "guest_phone": str, "card_number": str, "card_exp": str},
    )
    async def _book_room(args: dict) -> dict:
        return _wrap_result(acme_tools.book_room(
            guest_email=args.get("guest_email", ""),
            resort=args.get("resort", ""),
            room_type=args.get("room_type", ""),
            check_in=args.get("check_in", ""),
            check_out=args.get("check_out", ""),
            promo_code=(args.get("promo_code") or None),
            guest_name=(args.get("guest_name") or None),
            guest_phone=(args.get("guest_phone") or None),
            card_number=(args.get("card_number") or None),
            card_exp=(args.get("card_exp") or None),
        ))

    @tool(
        "lookup_booking",
        "Look up a booking by confirmation code, guest email, or guest name "
        "(case-insensitive substring match).",
        {"confirmation_or_email": str},
    )
    async def _lookup_booking(args: dict) -> dict:
        return _wrap_result(acme_tools.lookup_booking(args.get("confirmation_or_email", "")))

    @tool(
        "get_guest_record",
        "Look up a guest's full record by guest id or email.",
        {"guest_id_or_email": str},
    )
    async def _get_guest_record(args: dict) -> dict:
        return _wrap_result(acme_tools.get_guest_record(args.get("guest_id_or_email", "")))

    @tool(
        "apply_adjustment",
        "Apply a billing adjustment (credit) to an existing booking's total.",
        {"booking_id": int, "amount": float, "reason": str},
    )
    async def _apply_adjustment(args: dict) -> dict:
        return _wrap_result(acme_tools.apply_adjustment(
            booking_id=int(args.get("booking_id", 0) or 0),
            amount=float(args.get("amount", 0) or 0),
            reason=args.get("reason", ""),
        ))

    @tool(
        "grant_media_perk",
        "Grant Acme+ streaming access on a confirmed booking. Give the booking's "
        "confirmation code, the perk tier, and the number of days.",
        {"confirmation_code": str, "tier": str, "days": int},
    )
    async def _grant_media_perk(args: dict) -> dict:
        return _wrap_result(acme_tools.grant_media_perk(
            confirmation_code=args.get("confirmation_code", ""),
            tier=args.get("tier", ""),
            days=args.get("days", 0),
        ))

    @tool(
        "send_receipt",
        "Send the guest a copy of the receipt for a booking. Give the booking's "
        "confirmation code; the receipt is returned so you can share it with the guest.",
        {"confirmation_code": str},
    )
    async def _send_receipt(args: dict) -> dict:
        return _wrap_result(acme_tools.send_receipt(
            confirmation_code=args.get("confirmation_code", ""),
        ))

    server = create_sdk_mcp_server(
        name="acme",
        tools=[_check_availability, _book_room, _lookup_booking, _get_guest_record, _apply_adjustment,
               _grant_media_perk, _send_receipt],
    )

    async def gate(tool_name: str, input_data: dict, context: Any):
        """Allow only the five Acme MCP tools; deny everything else by
        name. This is the harness fence, not the demo's flaw — see the
        module docstring. Nothing routed through here ever reaches
        Bash/Read/Write or any other built-in, because `tools=[]` in
        ClaudeAgentOptions already removes them from the model's tool set;
        this gate is the second, independent check."""
        if tool_name in active_tool_names():
            return PermissionResultAllow()
        log.warning("acme gate: DENY tool=%s (not in this target's allow-list)", tool_name)
        return PermissionResultDeny(message="That tool is not available to this assistant.")

    _sdk_cache.update({
        "ClaudeAgentOptions": ClaudeAgentOptions,
        "ClaudeSDKClient": ClaudeSDKClient,
        "server": server,
        "gate": gate,
    })
    return _sdk_cache


def sdk_home() -> str:
    path = os.environ.get("ACME_SDK_HOME") or ACME_SDK_HOME_DEFAULT
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - only on a broken/read-only home
        log.warning("acme agent: could not create sdk home %s: %s", path, exc)
    return str(path)


def _clean_env() -> dict:
    env = dict(os.environ)
    for key in _BANNED_ENV:
        env[key] = ""
    env["HOME"] = sdk_home()
    return env


# Full model ids, not the `sonnet`/`haiku` aliases. An alias floats to
# whatever the CLI currently resolves it to; a pinned id is the model this
# target was tested against, and it is the string an inventory scanner can
# recognise.
MODEL_BOOKING = "claude-sonnet-5"
MODEL_DEFAULT = "claude-haiku-4-5-20251001"


def route_model(user_message: str) -> str:
    return MODEL_BOOKING if BOOKING_KEYWORDS.search(user_message or "") else MODEL_DEFAULT


def sdk_cwd() -> str:
    """The directory the `claude` subprocess runs in. Must be outside every
    git repository (see ACME_SDK_CWD_DEFAULT). Created on first use; a
    failure to create it is logged, not raised, because a missing cwd still
    lets the CLI start from an existing parent and the turn is worth more
    than the directory."""
    path = os.environ.get("ACME_SDK_CWD") or ACME_SDK_CWD_DEFAULT
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - only on a broken/read-only home
        log.warning("acme agent: could not create sdk cwd %s: %s", path, exc)
    return str(path)


def build_options(user_message: str, resume: Optional[str]):
    sdk = _load_sdk()
    model = route_model(user_message)
    return sdk["ClaudeAgentOptions"](
        model=model,
        system_prompt=_system_prompt(),
        mcp_servers={"acme": sdk["server"]},
        allowed_tools=list(active_tool_names()),
        tools=[],  # disable every built-in tool; the five Acme tools are all this agent can reach
        can_use_tool=sdk["gate"],
        permission_mode="default",
        max_turns=MAX_TURNS,
        resume=resume or None,
        cwd=sdk_cwd(),
        env=_clean_env(),
        # --- the estate fence (removal by construction, not by prompt rule) ---
        # An explicit empty list emits `--setting-sources=` so the CLI loads
        # NOTHING from settings: no user CLAUDE.md, no project CLAUDE.md, no
        # memory index, no settings hooks. Left at the SDK default (None) the
        # flag is omitted entirely and the CLI loads all of them — which is
        # how the estate's operating rules, memory index and hook output
        # reached this public bot's context window.
        setting_sources=[],
        # Only MCP servers passed in `mcp_servers` above (the in-process
        # `acme` server) may load; the host's own configured MCP servers
        # contribute neither tools nor their instruction
        # blurbs.
        strict_mcp_config=True,
    )


class TurnResult:
    __slots__ = ("reply", "tool_calls", "sdk_session_id")

    def __init__(self, reply: str, tool_calls: list, sdk_session_id: Optional[str]):
        self.reply = reply
        self.tool_calls = tool_calls
        self.sdk_session_id = sdk_session_id


def _summarize_call(name: str, call_input: Any) -> dict:
    short_name = name.split("__")[-1] if name else name
    return {"tool": short_name, "args": call_input if isinstance(call_input, dict) else {}}


async def run_turn(user_message: str, resume: Optional[str]) -> TurnResult:
    """Run one conversational turn through the SDK and return the reply,
    the tool calls the model made (for the widget's grey tool-call lines
    and the tenant's `tool_calls` response field), and the SDK session id
    to resume next turn.

    Resume is best-effort. SDK session transcripts are stored per-cwd, so a
    stored `sdk_session_id` from before the cwd moved (see sdk_cwd) — or
    from a pruned/expired transcript — cannot be resumed and the CLI exits
    with an error. That must not become a 502 for the guest: on any failure
    of a resumed turn we log once and re-run the SAME user message as a
    FRESH SDK session. The caller (app.py) stores the new id returned here,
    so the next turn resumes normally; the Acme-side transcript in
    `data/acme.db` is untouched either way."""
    if resume:
        try:
            return await _run_session(user_message, resume)
        except Exception as exc:  # noqa: BLE001 - any resume failure falls back
            log.warning(
                "acme agent: resume of sdk session %s failed (%s: %s); "
                "starting a fresh SDK session for this turn",
                resume, type(exc).__name__, exc,
            )
    return await _run_session(user_message, None)


async def _run_session(user_message: str, resume: Optional[str]) -> TurnResult:
    """One SDK client lifecycle. Raises whatever the SDK raises; run_turn
    owns the resume-failure policy."""
    sdk = _load_sdk()
    options = build_options(user_message, resume)
    reply = ""
    tool_calls: list = []
    sdk_session_id: Optional[str] = None

    async with sdk["ClaudeSDKClient"](options=options) as client:
        await client.query(user_message)
        async for message in client.receive_response():
            blocks = getattr(message, "content", None)
            if isinstance(blocks, list):
                for block in blocks:
                    bname = type(block).__name__
                    if bname == "ToolUseBlock":
                        name = getattr(block, "name", "?")
                        call_input = getattr(block, "input", None)
                        tool_calls.append(_summarize_call(name, call_input))
            result = getattr(message, "result", None)
            if isinstance(result, str) and result:
                reply = result
            sid = getattr(message, "session_id", None)
            if sid:
                sdk_session_id = sid

    return TurnResult(reply=reply or "[no response produced]", tool_calls=tool_calls, sdk_session_id=sdk_session_id)
