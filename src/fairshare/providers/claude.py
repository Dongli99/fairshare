"""Claude Code. Usage arrives on the statusLine command's stdin (documented), one account per
CLAUDE_CONFIG_DIR, and is asked for at launch through the tool's own `/usage` command. fairshare
never reads or copies a login.

Everything Anthropic may rename lives in the constants below and nowhere else in fairshare."""
from __future__ import annotations

import contextlib
import json
import os
import hashlib
import re
import shlex
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import store
from ..model import (EFFORT_NONE, NOTE_THINKING, NOTE_WRITING, USAGE_MODEL_TURN, USAGE_NO_ANSWER,
                     USAGE_NOT_UNDERSTOOD, USAGE_SIGNED_OUT, Account, AccountStatus, Candidate,
                     Capture, LiveSession, NoteEvent, SessionSeen, Window, model_window, plan_for,
                     window_names)

name = "claude"                      # fairshare's own id for this provider (accounts.toml, plans)
tool = "Claude Code"                 # the tool as users know it, for messages
web = "claude.ai"                    # where a subscription signs in and out in a browser
default_plan = "claude-pro"
# what setup offers to turn on in every session, for its question
remote_control = "Remote Control (follow and steer sessions from claude.ai/code or the Claude app)"

# The command-line tool.
BINARY = "claude"
command = BINARY
STATUS_ARGS = ("auth", "status", "--json")   # the sign-in check; accounts.toml may override it
LOGIN_ARGS = ("auth", "login")               # interactive: the tool owns the browser sign-in
LOGOUT_ARGS = ("auth", "logout")
VERSION_ARGS = ("--version",)
PROBE_TIMEOUT = 5.0                          # seconds
# The usage probe: the tool's own `/usage`, a local command (no model call, verified on 2.1.322,
# 2026-11-11), printed as a JSON envelope so that a release which sends it to the model instead
# shows up. The user's hooks are switched off for it: they would otherwise run at every launch,
# once per account (command-line settings beat the folder's own, verified the same day).
# accounts.toml may override it per account; `usage_args = []` turns it off.
USAGE_ARGS = ("-p", "--no-session-persistence", "--output-format", "json",
              "--settings", '{"disableAllHooks":true}', "/usage")
USAGE_TIMEOUT = 6.0                          # seconds; measured 1.1-2.4 s
usage_timeout = USAGE_TIMEOUT
# A pickup in the background: `claude --bg --name <name> <prompt>` starts a session with no
# terminal and prints how to attach to it. `--bg` passes the caller's environment on to the
# session (2.1.322, planner): only CONFIG_DIR_ENV arrives, so nothing else can travel that way.
# `--resume <id>` continues a session under its own id (any account can: the hub shares where
# the tool keeps sessions), in the foreground or with `--bg` (docs: CLI reference, /sessions and
# /agent-view, read 2026-11-12: on 2.1.257 or later the same id, or a copy with a `note:` line);
# `--fork-session` continues a copy under a new id. `--model <id>` and `--effort <level>` set the
# session's model and effort level (CLI reference, read 2026-11-12).
BG_ARGS, NAME_ARGS, RESUME_ARGS, FORK_ARGS = ("--bg",), ("--name",), ("--resume",), \
    ("--fork-session",)
MODEL_ARGS, EFFORT_ARGS = ("--model",), ("--effort",)
# The handoff note: one headless turn of a stopped session, resumed with the arguments above,
# in print mode with streamed JSON output and no tools (`--tools ""` disables every built-in
# tool). CLI reference, read 2026-11-12: `--include-partial-messages` requires `--print` and
# `--output-format stream-json`, its example also passes `--verbose`. One JSON object per line
# (2.1.322, planner, 2026-11-12): `system` lines (`thinking_tokens` while it thinks), then
# `stream_event` lines whose `event` is a `content_block_start` (`content_block.type`
# `thinking` or `text`) or a `content_block_delta` (`delta.type` `thinking_delta`, or
# `text_delta` with `delta.text`), among others; last, `{"type": "result", "is_error": ...,
# "result": <the answer's text>}` (/sessions, "Access conversations from scripts").
NOTE_ARGS = ("-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--tools", "")
NOTE_TYPE, NOTE_SUBTYPE, NOTE_EVENT = "type", "subtype", "event"
NOTE_SYSTEM, NOTE_THINKING_TOKENS, NOTE_STREAM = "system", "thinking_tokens", "stream_event"
NOTE_BLOCK_START, NOTE_BLOCK = "content_block_start", "content_block"
NOTE_DELTA_EVENT, NOTE_DELTA, NOTE_DELTA_TEXT = "content_block_delta", "delta", "text"
NOTE_THINKING_PARTS = ("thinking", "thinking_delta")
NOTE_TEXT_BLOCK, NOTE_TEXT_DELTA = "text", "text_delta"
# The running sessions: `claude agents --json`, items with `sessionId`, `name` and `status`
# (`busy` / `idle`), among others (`pid`, `cwd`, `kind`, `startedAt`); ended sessions are not
# listed (2.1.322, planner, 2026-11-11). A background session's name appeared within 4 s.
AGENTS_ARGS = ("agents", "--json")
# At its limit the tool tells the agent first (a user-side record: `[Usage limit reached; a short
# grace allowance remains, then this turn is cut off without warning. …]`), then ends the turn
# (`isApiErrorMessage`, `error: "rate_limit"`, `You've hit your 5-hour limit · resets 8:40pm
# (Zone)`). After the reset it queues, by itself, `I hit my usage limit while you were working,
# but it has reset now. Please continue from where you left off.` (`promptSource: "queued"`)
# and the session carries on. Observed, not documented: 2.1.322, 2026-11-11 (planner; a
# background and an interactive session). fairshare reads none of these texts; it relies only on
# the resume, to leave a session its limit stopped waiting for the reset (the default;
# `resume_wait_minutes` opts in to a move).
RESUMES_AT_RESET = True
resumes_at_reset = RESUMES_AT_RESET
AGENT_ID, AGENT_NAME, AGENT_STATUS, AGENT_BUSY, AGENT_IDLE = (
    "sessionId", "name", "status", "busy", "idle")

# Environment variables.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"         # picks the account's folder
# Variables that pick a login or a provider: dropped, and the launcher says so (names only).
LOGIN_OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                      "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                      "CLAUDE_CODE_USE_FOUNDRY")
# What a running session injects into its children: dropped silently. Everything else, such as
# CLAUDE_CODE_MAX_OUTPUT_TOKENS, is the user's and passes through. Verified on Claude Code
# 2.1.322, 2026-11-09.
SESSION_MARK_ENV = "CLAUDECODE"              # set in every child of a running session
SESSION_ENV = (SESSION_MARK_ENV, "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_JOB_DIR",
               "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
               "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
               "CLAUDE_CODE_SESSION_ID")
WATCH_ENV_PREFIX = "CLAUDE_"                 # inside a session, other such variables are reported
# Of those, what a command run inside a session (a tool's shell, `!`) learns of it: the session's
# full id, and the session's own process (2.1.322, planner).
SESSION_ID_ENV, SESSION_PID_ENV = "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"

# Files and folders.
SETTINGS_FILE = "settings.json"      # holds the statusLine hook
# What every account shares through the hub; a name ending in "/" is a folder. Everything else in
# an account's folder (its login, STATE_FILE, caches, jobs, locks) stays its own. Observed in real
# config dirs on 2.1.322; CLAUDE.md, agents/, commands/, output-styles/ and keybindings.json are
# documented as user-level files (code.claude.com/docs/en/claude-directory, 2026-11-09), and so
# are rules/, workflows/, agent-memory/ and themes/ (same page, 2026-11-10).
SHARED = ("projects/", "history.jsonl", "file-history/", "paste-cache/", "plugins/", "skills/",
          "tasks/", SETTINGS_FILE, "sessions/", "CLAUDE.md", "agents/", "commands/",
          "output-styles/", "keybindings.json", "rules/", "workflows/", "agent-memory/",
          "themes/")
shared = SHARED
# Shared, but never copied in by join: the sessions running now, each with its own key. A copy
# would be stale on arrival and would carry another session's key.
NOT_COPIED = ("sessions/",)
not_copied = NOT_COPIED
STATE_FILE = ".claude.json"          # the tool's own state, inside CONFIG_DIR_ENV's folder
STATE_LOCK = STATE_FILE + ".lock"    # the folder the tool holds while it writes STATE_FILE
STATE_LOCK_WAIT = 10.0               # seconds fairshare waits for that folder
STATE_LOCK_STALE = 30.0              # a lock folder untouched this long is left over: the tool
                                     # leaves one behind at times (`auth status`, 2.1.322) and
                                     # writes past an old one itself (one seen 25 s old)
DEFAULT_DIR = ".claude"              # the folder used without CONFIG_DIR_ENV; its STATE_FILE is
                                     # beside it in HOME
default_dir = "~/" + DEFAULT_DIR
ACCOUNT_DIR_GLOB = ".claude*"        # folders under HOME that may be accounts (discovery only)
RC_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".bash_aliases",
            ".profile")              # read-only, for aliases that set CONFIG_DIR_ENV
_ALIAS = re.compile(r"""^\s*alias\s+([\w.-]+)=["']?\s*(?:env\s+)?"""
                    + re.escape(CONFIG_DIR_ENV) + r"""=("[^"]*"|'[^']*'|[^\s"';]+)""")

# JSON fields: the statusLine entry in the settings file, and the usage payload it is given.
STATUS_LINE_KEY = "statusLine"
HOOK_TYPE_KEY, HOOK_TYPE = "type", "command"
HOOK_COMMAND_KEY = "command"
SETTINGS_ENV = "env"                 # variables the settings file sets: values never shown
CAPTURE_LIMITS, CAPTURE_USED, CAPTURE_RESET = "rate_limits", "used_percentage", "resets_at"
# The session, in the statusLine payload and in every hook's input alike (documented:
# code.claude.com/docs/en/statusline and /hooks, read 2026-11-11).
SESSION_ID, SESSION_TRANSCRIPT, SESSION_CWD = "session_id", "transcript_path", "cwd"
# The statusLine payload's model (`model.id`, `model.display_name`) and effort level
# (`effort.level`: the session's level, the one actually run when the model does not support
# the one set; absent when the model takes no effort level). Documented: /statusline, read
# 2026-11-12.
CAPTURE_MODEL, CAPTURE_MODEL_ID, CAPTURE_MODEL_NAME = "model", "id", "display_name"
CAPTURE_EFFORT, CAPTURE_EFFORT_LEVEL = "effort", "level"
# The session's running cost (`cost.total_cost_usd`): an estimate at list price, computed by the
# tool, not a bill; it resets to 0 when `/clear` starts a new session. Documented: /statusline,
# read 2026-11-12.
CAPTURE_COST, CAPTURE_COST_TOTAL = "cost", "total_cost_usd"
# The statusLine re-runs on a timer as well as on events (seconds, minimum 1; documented). The bar
# then re-reads the local snapshot once a minute, which costs nothing.
STATUS_REFRESH_KEY, STATUS_REFRESH_S = "refreshInterval", 60
# A boolean in the settings file: start Remote Control in every session ("Start Remote Control
# bridge automatically each session", the settings schema in the binary, 2.1.327, 2026-11-17).
# Organization policy may lock it, and some organizations turn it on by default.
REMOTE_CONTROL_KEY = "remoteControlAtStartup"

# fairshare's own hooks in the settings file: `hooks.<event>` is a list of groups, each a
# `matcher` and a `hooks` list of commands (documented, read 2026-11-11). Keyed by fairshare's
# name for the hook (`fairshare hook <name>`): the tool's event and the matcher (None: the
# event takes none, and the group is written without one). StopFailure fires instead of Stop
# when an API error ended the turn; its output and exit code are ignored, and its input names
# the error in HOOK_ERROR. PostToolUse (after every tool call; "*" matches every tool) and
# UserPromptSubmit (no matcher support: one would be ignored) take a JSON answer whose
# `hookSpecificOutput.additionalContext` is added to the model's context (documented: /hooks,
# read 2026-11-11). Every hook's input names a subagent or teammate it fired in by `agent_id`.
HOOKS_KEY, HOOK_MATCHER_KEY, HOOK_LIST_KEY = "hooks", "matcher", "hooks"
STOP_FAILURE, RATE_LIMIT = "StopFailure", "rate_limit"
POST_TOOL, ALL_TOOLS, PROMPT = "PostToolUse", "*", "UserPromptSubmit"
HOOK_ERROR, HOOK_AGENT = "error", "agent_id"
HOOK_OUTPUT, HOOK_EVENT_NAME, HOOK_CONTEXT = "hookSpecificOutput", "hookEventName", "additionalContext"
FAIRSHARE_HOOKS = {"stop-failure": (STOP_FAILURE, RATE_LIMIT), "post-tool": (POST_TOOL, ALL_TOOLS),
                   "prompt": (PROMPT, None)}
CONTEXT_EVENTS = (POST_TOOL, PROMPT)  # the events whose hooks may speak to the model

# fairshare's skill, which teaches main sessions to use workers, watch and checkpoints: a folder
# holding SKILL.md, whose front matter (between `---` lines) gives `name` and `description`
# (documented: code.claude.com/docs/en/skills, read 2026-11-11). Shipped as package data beside
# this file, installed as the hub's shared `skills/fairshare/`, recognised by its `name`.
SKILLS_DIR, SKILL_FILE, SKILL_NAME = "skills", "SKILL.md", "fairshare"
SKILL_SOURCE = "claude_skill"        # the shipped folder, in this package
SKILL_FENCE, SKILL_NAME_KEY = "---", "name"

# JSON fields of STATE_FILE that join may carry over from a user's folder, after asking, and nothing
# else (never identity, tokens or account fields). Onboarding is marked done by these two keys, set
# together when the first-run screens finish (2.1.322); the theme chosen there goes to
# SETTINGS_FILE. Folder trust is `projects.<path>.hasTrustDialogAccepted`, per account; so is the
# Claude in Chrome consent (seen 2026-11-10).
STATE_PROJECTS, STATE_TRUST = "projects", "hasTrustDialogAccepted"
STATE_MCP = "mcpServers"
STATE_ONBOARDING = ("hasCompletedOnboarding", "lastOnboardingVersion")
STATE_CHROME = ("hasCompletedClaudeInChromeOnboarding", "claudeInChromeDefaultEnabled")

# Which folder's trust covers a working folder (verified on 2.1.322, 2026-11-10, throwaway dirs):
# the folder and its parents up to the root of the git repository it is in (inclusive), plus the
# main repository's root for a git worktree; outside a repository, every parent. The tool's own
# dialog writes the main repository's root when accepted inside a repository, else the folder.
# Paths are the OS-canonical spelling. These name git's own files.
GIT_DIR = ".git"                     # a folder in a repository's root; a file in a worktree's
GIT_FILE_PREFIX = "gitdir:"          # that file's one line names the worktree's git folder
GIT_COMMONDIR = "commondir"          # in which this file names the main repository's .git

# JSON fields and values of the sign-in check's output.
STATUS_LOGGED_IN, STATUS_METHOD = "loggedIn", "authMethod"
STATUS_SUBSCRIPTION = "claude.ai"            # the STATUS_METHOD of a subscription login
STATUS_ORG, STATUS_EMAIL, STATUS_PLAN = "orgId", "email", "planTier"
STATUS_EXPIRES = "loginExpiresAt"            # ISO time the sign-in ends; null when signed out

# The usage probe's JSON envelope, and the text in its `result` (or on stdout without the
# envelope). Only lines starting with a known label are read: `<label>: N% used · resets <when>`,
# where <when> is `Nov 11 at 3:40pm (Zone/Name)` (2.1.322) or a bare `9pm` (2.1.321). The rest
# (tips, with local paths) is never parsed or kept. A signed-out folder answers with the cost
# summary instead (2.1.322, 2026-11-11).
USAGE_RESULT, USAGE_IS_ERROR = "result", "is_error"
USAGE_LOCAL, USAGE_LOCAL_NAME, USAGE_TURNS = "local_command", "usage", "num_turns"
USAGE_COST = "total_cost_usd"
USAGE_SESSION = "Current session"
USAGE_WEEK = "Current week (all models)"
USAGE_MODEL_WEEK = "Current week ({})"       # {}: the model's name, e.g. Fable
USAGE_WINDOWS = {USAGE_SESSION: "five_hour", USAGE_WEEK: "seven_day"}   # checked against plans
USAGE_COST_VIEW = "Total cost:"              # the signed-out answer's first words
USAGE_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                "dec")


def _spaced(text: str) -> str:
    """A pattern for `text` that takes any run of spaces where it has one."""
    return re.escape(text).replace(r"\ ", r"\s+")


_MODEL_LABEL = (_spaced(USAGE_MODEL_WEEK.partition("{}")[0]) + r"\s*(?P<model>[^()]+?)\s*"
                + _spaced(USAGE_MODEL_WEEK.partition("{}")[2]))
_USAGE_LINE = re.compile(r"^\s*(?P<label>" + "|".join(_spaced(x) for x in USAGE_WINDOWS)
                         + "|" + _MODEL_LABEL + r")\s*:\s*(?P<pct>\d+(?:\.\d+)?)\s*%\s*(?:used)?"
                         r"\s*(?:\u00b7\s*resets\s+(?P<when>.*?))?\s*$", re.I)
# "Nov 17 at 4pm" on macOS; "Nov 17, 4pm" from the same release on Linux (2.1.326)
_WHEN = re.compile(r"(?:(?P<month>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:\s+at\s+|\s*,\s*))?"
                   r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)"
                   r"\s*(?:\((?P<zone>[^()]+)\))?", re.I)


def parse_capture(raw: dict) -> dict[str, Window]:
    known = window_names(name)               # the plan files say which windows exist
    out = {}
    for key, w in (raw.get(CAPTURE_LIMITS) or {}).items():
        if key in known and isinstance(w, dict) and CAPTURE_USED in w and CAPTURE_RESET in w:
            out[key] = Window(float(w[CAPTURE_USED]), float(w[CAPTURE_RESET]))
    return out


def _str(d: object, key: str) -> str | None:
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, str) and v else None


def _session(raw: object) -> SessionSeen | None:
    """The session a hook input (or a statusLine payload) names: id, folder, transcript."""
    if (sid := _str(raw, SESSION_ID)) is None:
        return None
    return SessionSeen(sid, _str(raw, SESSION_CWD), _str(raw, SESSION_TRANSCRIPT))


def session_of(raw: object) -> SessionSeen | None:
    """The session a statusLine payload describes, with its running cost when given and its
    model and effort level when the payload names a model; else None."""
    if (seen := _session(raw)) is None:
        return None
    cost = raw[CAPTURE_COST].get(CAPTURE_COST_TOTAL) if isinstance(raw.get(CAPTURE_COST),
                                                                   dict) else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        seen = replace(seen, cost=float(cost))
    if (model := _str(raw.get(CAPTURE_MODEL), CAPTURE_MODEL_ID)) is None:
        return seen
    return replace(seen, model=model, model_name=_str(raw[CAPTURE_MODEL], CAPTURE_MODEL_NAME),
                   effort=_str(raw.get(CAPTURE_EFFORT), CAPTURE_EFFORT_LEVEL) or EFFORT_NONE)


def current_session() -> str | None:
    """The id of the session this process runs inside, else None."""
    return os.environ.get(SESSION_ID_ENV) or None


def session_pid() -> int | None:
    """The process id of the session this process runs inside, else None."""
    try:
        return int(os.environ[SESSION_PID_ENV])
    except (KeyError, ValueError):
        return None


def resume_args(session_id: str, fork: bool = False) -> list[str]:
    return [*RESUME_ARGS, session_id, *(FORK_ARGS if fork else ())]


def model_args(model: str | None, effort: str | None) -> list[str]:
    """Arguments that start a session on `model` at `effort` (EFFORT_NONE or None: no effort
    argument); none for what is None."""
    return [*((*MODEL_ARGS, model) if model else ()),
            *((*EFFORT_ARGS, effort) if effort and effort != EFFORT_NONE else ())]


def uses_week(model: str, week: str) -> bool:
    """Whether the per-model week the usage report names `week` (`Fable`) limits `model` (an
    id such as `claude-fable-5-2`, an alias or a display name): every word of the week's name
    is in the model's."""
    words = week.lower().split()
    return bool(words) and all(w in model.lower() for w in words)


def background(account: Account, name: str, prompt: str, extra: list[str] = ()
               ) -> tuple[list[str], dict[str, str]]:
    """The command and environment that start a session of `account` with no terminal, named
    `name`, with `extra` arguments (a resume, a model) and `prompt` as its first message."""
    return ([real_binary(), *BG_ARGS, *NAME_ARGS, name, *extra, prompt],
            clean_env(account.config_dir))


def note_command(account: Account, session_id: str, extra: list[str], prompt: str
                 ) -> tuple[list[str], dict[str, str]]:
    """One headless turn of the stopped session `session_id` on `account`: no tools, streamed
    JSON out, with `extra` arguments (a model) and `prompt` as the turn's message. The id stays
    the same."""
    return ([real_binary(), *NOTE_ARGS, *resume_args(session_id), *extra, prompt],
            clean_env(account.config_dir))


def note_event(line: str) -> NoteEvent | None:
    """What one line of that turn's output says: thinking, or writing (with the new text); None
    for any other line, or one that does not parse."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get(NOTE_TYPE) == NOTE_SYSTEM:
        return NoteEvent(NOTE_THINKING) if d.get(NOTE_SUBTYPE) == NOTE_THINKING_TOKENS else None
    e = d.get(NOTE_EVENT) if d.get(NOTE_TYPE) == NOTE_STREAM else None
    part = e.get({NOTE_BLOCK_START: NOTE_BLOCK, NOTE_DELTA_EVENT: NOTE_DELTA}.get(
        e.get(NOTE_TYPE), "")) if isinstance(e, dict) else None
    kind = part.get(NOTE_TYPE) if isinstance(part, dict) else None
    if kind in NOTE_THINKING_PARTS:
        return NoteEvent(NOTE_THINKING)
    if kind == NOTE_TEXT_BLOCK:
        return NoteEvent(NOTE_WRITING)
    if kind == NOTE_TEXT_DELTA:
        return NoteEvent(NOTE_WRITING, _str(part, NOTE_DELTA_TEXT) or "")
    return None


def note_text(out: str) -> str | None:
    """The answer's text in that turn's final output line (a JSON document); None for an error
    or no text."""
    try:
        d = json.loads(out)
    except ValueError:
        return None
    text = _str(d, USAGE_RESULT)
    return None if not isinstance(d, dict) or d.get(USAGE_IS_ERROR) is True or not text \
        else text.strip() or None


def list_sessions(account: Account) -> list[LiveSession] | None:
    """The sessions running now, as the tool lists them (any account's folder sees them all:
    the hub shares where the tool keeps them); None when the list could not be had."""
    try:
        r = subprocess.run([real_binary(), *AGENTS_ARGS], env=clean_env(account.config_dir),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT)
        raw = json.loads(r.stdout)
    except (SystemExit, OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(raw, dict):            # a list inside an object would do as well
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        return None
    status = {AGENT_BUSY: True, AGENT_IDLE: False}
    return [LiveSession(i[AGENT_ID], i.get(AGENT_NAME) if isinstance(i.get(AGENT_NAME), str)
                        else None, status.get(i.get(AGENT_STATUS)))
            for i in raw if isinstance(i, dict) and isinstance(i.get(AGENT_ID), str)]


# The transcript (`transcript_path`, internal and per version: one JSON record per line; fixture
# tests/fixtures/claude-2.1.322/transcript.jsonl). Only records of these types that are not on a
# side chain (a subagent's) are the conversation; their text is the message's content, a string
# or a list of blocks of which only the text blocks are words (thinking, tool use and tool results
# are not). Every other record type (copies of requests among them) is never shown.
T_TYPE, T_SIDECHAIN, T_MESSAGE, T_CONTENT = "type", "isSidechain", "message", "content"
T_ROLES = {"user": "user", "assistant": "assistant"}     # record type -> fairshare's role
T_BLOCK_TYPE, T_TEXT_BLOCK, T_TEXT = "type", "text", "text"
# A `!` command typed in a session is a user message of its own, `<bash-input>cmd</bash-input>`,
# and its output another, `<bash-stdout>...</bash-stdout>` with `<bash-stderr>...</bash-stderr>`
# (2.1.322, planner, 2026-11-11). They become the roles "command" and "output".
T_BANG_INPUT, T_BANG_OUTPUT = "bash-input", ("bash-stdout", "bash-stderr")


def _tagged(text: str, tag: str) -> str | None:
    """What `<tag>...</tag>` holds in `text`, else None."""
    m = re.search(f"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1) if m else None


def _bang(text: str) -> tuple[str, str] | None:
    """A `!` command's message as ("command", the command) or ("output", what it printed),
    else None."""
    if text.startswith(f"<{T_BANG_INPUT}>") and (cmd := _tagged(text, T_BANG_INPUT)) is not None:
        return "command", cmd.strip()
    found = [x for tag in T_BANG_OUTPUT if (x := _tagged(text, tag)) is not None]
    if found and text.startswith(tuple(f"<{tag}>" for tag in T_BANG_OUTPUT)):
        return "output", "\n".join(x.strip("\n") for x in found if x.strip())
    return None


def _text_of(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b[T_TEXT] for b in content if isinstance(b, dict)
                            and b.get(T_BLOCK_TYPE) == T_TEXT_BLOCK and isinstance(b.get(T_TEXT), str))
    return None


def transcript_text(path: Path) -> list[tuple[str, str]] | None:
    """The main conversation of a transcript, oldest first, as (role, text) pairs with role
    "user" or "assistant", or "command" and "output" for a `!` command the user ran: only
    words, never tool calls, tool results, thinking, side chains or other records. [] when it
    holds no such message; None when it cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get(T_TYPE) not in T_ROLES \
                or r.get(T_SIDECHAIN) is not False or not isinstance(r.get(T_MESSAGE), dict):
            continue
        if (text := _text_of(r[T_MESSAGE].get(T_CONTENT))) and text.strip():
            role = T_ROLES[r[T_TYPE]]
            bang = _bang(text.strip()) if role == "user" else None
            if bang is None:
                out.append((role, text.strip()))
            elif bang[1]:
                out.append(bang)
    return out


def hook_session(hook: str, raw: object) -> SessionSeen | None:
    """For fairshare's hook `hook`: the session its input describes, when the input is the event
    fairshare wired it for (an input naming another error is not); else None. A hook that
    speaks to the model gets None for an input from inside a subagent: the main session
    decides."""
    if hook not in FAIRSHARE_HOOKS or not isinstance(raw, dict):
        return None
    event, matcher = FAIRSHARE_HOOKS[hook]
    if event in CONTEXT_EVENTS:
        if raw.get(HOOK_AGENT) is not None:
            return None
    elif (error := raw.get(HOOK_ERROR)) is not None and error != matcher:
        return None
    return _session(raw)


def hook_output(hook: str, text: str) -> str:
    """What fairshare's hook `hook` prints so that `text` reaches the model as context."""
    return json.dumps({HOOK_OUTPUT: {HOOK_EVENT_NAME: FAIRSHARE_HOOKS[hook][0],
                                     HOOK_CONTEXT: text}}, ensure_ascii=False)


def launch(account: Account) -> tuple[list[str], dict[str, str]]:
    return [real_binary()], clean_env(account.config_dir)


def login_command(config_dir: Path) -> tuple[list[str], dict[str, str]]:
    return [real_binary(), *LOGIN_ARGS], clean_env(config_dir)


def logout_command(config_dir: Path) -> tuple[list[str], dict[str, str]]:
    return [real_binary(), *LOGOUT_ARGS], clean_env(config_dir)


def session_dir() -> Path | None:
    """The account folder of the session this process runs in (a usage hook's), else None."""
    d = os.environ.get(CONFIG_DIR_ENV)
    return Path(d) if d else None


def _scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in LOGIN_OVERRIDE_ENV and k not in SESSION_ENV}


def clean_env(config_dir: Path) -> dict[str, str]:
    """The environment for a child claude of one account: this process's, minus every variable
    that would pick another login or carry another session's state, with the account's dir."""
    return {**_scrubbed_env(), CONFIG_DIR_ENV: str(config_dir)}


def dropped_login_vars() -> list[str]:
    return [k for k in LOGIN_OVERRIDE_ENV if k in os.environ]


def passed_session_vars() -> list[str]:
    """Inside a running session: names of the other CLAUDE_* variables the child will inherit,
    so a session variable a new release adds shows up instead of silently misleading the child.
    Empty outside a session."""
    if SESSION_MARK_ENV not in os.environ:
        return []
    return sorted(k for k in os.environ if k.startswith(WATCH_ENV_PREFIX)
                  and k not in SESSION_ENV and k not in LOGIN_OVERRIDE_ENV and k != CONFIG_DIR_ENV)


def _iso(value: object) -> float | None:
    """Unix seconds of an ISO time with a zone (`2030-01-02T03:04:05.678Z`), else None."""
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return t.timestamp() if t.tzinfo else None
    except (AttributeError, ValueError):
        return None


def parse_account_status(raw: object) -> AccountStatus | None:
    """The status JSON as an AccountStatus; None when it is not the shape we know."""
    if not isinstance(raw, dict) or not isinstance(raw.get(STATUS_LOGGED_IN), bool):
        return None
    if not raw[STATUS_LOGGED_IN]:
        return AccountStatus(False, False)
    org, email = raw.get(STATUS_ORG), raw.get(STATUS_EMAIL)
    who = hashlib.sha256(f"{org}\n{email}".encode()).hexdigest() if org and email else None
    return AccountStatus(True, raw.get(STATUS_METHOD) == STATUS_SUBSCRIPTION, who,
                         plan_for(name, raw.get(STATUS_PLAN)), _iso(raw.get(STATUS_EXPIRES)))


def _account_env(account: Account) -> dict[str, str]:
    """The environment for asking the tool about `account`: for the user's DEFAULT_DIR without
    CONFIG_DIR_ENV, as the tool itself runs there (setting it may name a different login
    entry)."""
    env = clean_env(account.config_dir)
    if account.config_dir == Path.home() / DEFAULT_DIR:
        del env[CONFIG_DIR_ENV]
    return env


def account_status(account: Account) -> AccountStatus | None:
    """Ask the installed claude who `account` is. Reads no login file; the output is parsed in
    memory and only its yes/no, a hash, a plan name and the sign-in's end survive. None on any
    failure. The exit code is not read: a signed-out folder prints its answer and exits 1
    (2.1.322)."""
    env = _account_env(account)
    try:
        r = subprocess.run([real_binary(), *(account.status_args or STATUS_ARGS)],
                           env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        return parse_account_status(json.loads(r.stdout))
    except (SystemExit, OSError, subprocess.SubprocessError, ValueError):
        return None


def _reset_at(when: str, now: float) -> float | None:
    """Unix seconds of a printed reset (`Nov 12 at 3am (America/Denver)`, or a bare `9pm`), in
    the zone it names (local time for a missing or unknown one); None when it cannot be read.
    There is no year: the first candidate after `now - SAME_WINDOW_S` (the printed minute jitters,
    so a reset a minute ago is that one, not next year's or tomorrow's); a bare clock time is its
    next occurrence on that rule."""
    m = _WHEN.fullmatch(when.strip())
    if not m or int(m["hour"]) > 12 or int(m["minute"] or 0) > 59:
        return None
    try:
        zone = ZoneInfo(m["zone"].strip()) if m["zone"] else None
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        zone = None
    hour = int(m["hour"]) % 12 + (12 if m["half"].lower() == "pm" else 0)
    minute = int(m["minute"] or 0)
    base = datetime.fromtimestamp(now, zone).replace(hour=hour, minute=minute, second=0,
                                                    microsecond=0)
    if not m["month"]:
        days = [base + timedelta(days=d) for d in (-1, 0, 1)]
    elif (month := m["month"].lower()) not in USAGE_MONTHS:
        return None
    else:
        days = []
        for year in (base.year - 1, base.year, base.year + 1):
            with contextlib.suppress(ValueError):           # Feb 29 in a common year
                days.append(base.replace(year=year, month=USAGE_MONTHS.index(month) + 1,
                                         day=int(m["day"])))
    return next((t for d in days if (t := d.timestamp()) > now - store.SAME_WINDOW_S), None)


def parse_usage(text: str, now: float) -> dict[str, Window]:
    """The windows in `/usage` text, keyed as parse_capture keys them, plus per-model weeks under
    `model_window`. A line with no readable reset is dropped: a window without one is useless."""
    known, out = window_names(name), {}
    for line in text.splitlines():
        if not (m := _USAGE_LINE.match(line)) or not m["when"]:
            continue
        said = " ".join(m["label"].split()).lower()
        label = next((k for k in USAGE_WINDOWS if k.lower() == said), None)
        key = USAGE_WINDOWS[label] if label else model_window(USAGE_WINDOWS[USAGE_WEEK],
                                                              m["model"].strip())
        if (label and key not in known) or (at := _reset_at(m["when"], now)) is None:
            continue
        out[key] = Window(float(m["pct"]), at)
    return out


def read_usage(account: Account, now: float) -> dict[str, Window] | str | None:
    """Ask the installed claude for `account`'s usage (USAGE_ARGS, or the account's own
    `usage_args`). The windows it read; else a short, value-free reason (a string); None when the
    account's `usage_args` turn the probe off. Never raises. Runs like account_status: clean
    environment, stdin from /dev/null, in this process's folder. With the JSON envelope, an answer
    that was not the local command (the model ran) gives USAGE_MODEL_TURN and no windows; plain
    text (an override without the envelope) is read too, but then that guard is blind."""
    args = USAGE_ARGS if account.usage_args is None else account.usage_args
    if not args:
        return None
    try:
        r = subprocess.run([real_binary(), *args], env=_account_env(account),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=USAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"{USAGE_NO_ANSWER}: timed out"
    except (SystemExit, OSError, subprocess.SubprocessError, ValueError):
        return f"{USAGE_NO_ANSWER}: could not run {BINARY}"
    try:
        envelope = json.loads(r.stdout)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        # A model turn shows as turns or cost, never as a missing field alone: an error answer
        # (no network, say) must not tell the user to switch the probe off.
        turns, cost = envelope.get(USAGE_TURNS), envelope.get(USAGE_COST)
        if (isinstance(turns, int) and turns > 0) or (isinstance(cost, (int, float)) and cost > 0):
            return USAGE_MODEL_TURN
        text = envelope.get(USAGE_RESULT)
        if envelope.get(USAGE_IS_ERROR) is True or not isinstance(text, str):
            return f"{USAGE_NO_ANSWER}: error"
        if envelope.get(USAGE_LOCAL) != USAGE_LOCAL_NAME:
            return USAGE_NOT_UNDERSTOOD
    elif r.returncode != 0:
        return f"{USAGE_NO_ANSWER}: exit {r.returncode}"
    elif not (text := r.stdout).strip():
        return f"{USAGE_NO_ANSWER}: no output"
    if windows := parse_usage(text, now):
        return windows
    return USAGE_SIGNED_OUT if text.lstrip().startswith(USAGE_COST_VIEW) else USAGE_NOT_UNDERSTOOD


def find_binary(path: str | None = None) -> str | None:
    """The first BINARY on PATH (or on `path`, when given)."""
    return shutil.which(BINARY, path=path)


def real_binary(path: str | None = None) -> str:
    if (found := find_binary(path)) is None:
        raise SystemExit(f"fairshare: no {BINARY} found on PATH")
    return found


def version() -> str | None:
    """The first line the tool prints for VERSION_ARGS, in a scrubbed environment; None on any
    failure."""
    try:
        r = subprocess.run([real_binary(), *VERSION_ARGS], env=_scrubbed_env(),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT)
        lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
        return lines[0][:80] if lines else None
    except (SystemExit, OSError, subprocess.SubprocessError):
        return None


def discover(home: Path) -> list[Candidate]:
    """Folders set up under `home`: aliases of the form `alias X="CONFIG_DIR_ENV=P ..."` in
    shell rc files (best effort), and ACCOUNT_DIR_GLOB dirs the tool has run in: holding their
    own STATE_FILE, or DEFAULT_DIR with HOME's beside it. Shared items alone (a folder of shared
    sessions, say) do not make an account. DEFAULT_DIR first, then by id. Reads no login."""
    found: dict[Path, Candidate] = {}
    for rc in RC_FILES:
        try:
            lines = (home / rc).read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if m := _ALIAS.match(line):
                d = _expand(m.group(2).strip("\"'"), home)
                found.setdefault(d, Candidate(m.group(1), name, d, f"alias in ~/{rc}"))
    for d in sorted(home.glob(ACCOUNT_DIR_GLOB)):
        if d in found or not d.is_dir():
            continue
        if (d / STATE_FILE).is_file() or (d == home / DEFAULT_DIR
                                          and (home / STATE_FILE).is_file()):
            found[d] = Candidate(d.name.lstrip("."), name, d, f"folder ~/{d.name}")
    return sorted(found.values(), key=lambda c: (c.config_dir != home / DEFAULT_DIR, c.id))


def _expand(p: str, home: Path) -> Path:
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if p.startswith(prefix):
            return home / p[len(prefix):]
    return home if p in ("~", "$HOME", "${HOME}") else Path(p)


# --- The tool's state file: which one a folder uses, and what join may carry over from it.

def state_file(folder: Path, home: Path) -> Path:
    """The STATE_FILE that `folder` uses: its own, or for DEFAULT_DIR (used without
    CONFIG_DIR_ENV) the one beside it in HOME."""
    inside = folder / STATE_FILE
    if not inside.exists() and folder == home / DEFAULT_DIR:
        return home / STATE_FILE
    return inside


def state_lock(state: Path) -> Path:
    """The folder the tool holds while it writes `state`; fairshare takes it too."""
    return state.with_name(STATE_LOCK)


def hold_state(state: Path):
    """Hold the tool's own lock folder for `state` while the block runs, as the tool does: wait
    up to STATE_LOCK_WAIT seconds, take over one left over (older than STATE_LOCK_STALE), else
    raise OSError."""
    return store.held(state_lock(state), STATE_LOCK_WAIT, STATE_LOCK_STALE)


def _read_state(state: Path) -> dict:
    """STATE_FILE's object ({} when there is none). Raises for one that cannot be read or decoded:
    it is never taken for absent."""
    if not state.exists():
        return {}
    d = json.loads(state.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError("not a JSON object")
    return d


def _git_root(path: Path) -> Path | None:
    """The root of the git repository (or worktree) `path` is in, else None."""
    for p in (path, *path.parents):
        if os.path.lexists(p / GIT_DIR):
            return p
    return None


def _main_root(root: Path) -> Path:
    """For a git worktree's root, the main repository's root; otherwise `root` itself."""
    dot = root / GIT_DIR
    if not dot.is_file():
        return root
    try:
        line = dot.read_text(encoding="utf-8").strip()
        if not line.startswith(GIT_FILE_PREFIX):
            return root
        gitdir = (root / line[len(GIT_FILE_PREFIX):].strip()).resolve()
        common = (gitdir / (gitdir / GIT_COMMONDIR).read_text(encoding="utf-8").strip()).resolve()
    except (OSError, UnicodeDecodeError):
        return root                          # a submodule, or something else: its own root
    return common.parent if common.name == GIT_DIR else root


def trust_paths(path: Path) -> list[Path]:
    """The folders whose trust covers the canonical folder `path`, as the tool looks them up."""
    root = _git_root(path)
    if root is None:
        return [path, *path.parents]
    up = [path, *(p for p in path.parents if p.is_relative_to(root))]
    main = _main_root(root)
    return up + ([main] if main != root else [])


def trust_key(path: Path) -> Path:
    """The folder whose entry the tool's own dialog writes when it is accepted in `path`."""
    root = _git_root(path)
    return _main_root(root) if root else path


def trusts(config_dir: Path, path: Path) -> bool | None:
    """Whether the account folder `config_dir` trusts the canonical folder `path`; None when its
    STATE_FILE cannot be read or decoded."""
    try:
        projects = _read_state(config_dir / STATE_FILE).get(STATE_PROJECTS)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(projects, dict):
        return False
    return any(isinstance(e := projects.get(str(p)), dict) and e.get(STATE_TRUST) is True
               for p in trust_paths(path))


def trust(config_dir: Path, path: Path, backups: Path) -> Path:
    """Make the account folder `config_dir` trust the canonical folder `path`, writing the entry
    where the tool itself would (`trust_key`), and return that folder. Under the tool's lock: a
    file that cannot be read or decoded is refused, the old one is backed up into `backups`,
    only that key is set (every other key and value kept), the file is replaced atomically and
    read back. A missing STATE_FILE is created holding only that entry. Raises OSError or
    ValueError and writes nothing when it cannot."""
    state, key = config_dir / STATE_FILE, trust_key(path)
    with hold_state(state):
        d = _read_state(state)
        projects = d.get(STATE_PROJECTS, {})
        entry = projects.get(str(key), {}) if isinstance(projects, dict) else None
        if not isinstance(entry, dict):
            raise ValueError(f"{state}: unexpected layout under {STATE_PROJECTS}")
        new = {**d, STATE_PROJECTS: {**projects, str(key): {**entry, STATE_TRUST: True}}}
        store.write_with_backup(state, json.dumps(new, indent=2, ensure_ascii=False) + "\n",
                                backups)
        if _read_state(state) != new:
            raise OSError(f"{state} did not read back as written")
    return key


def check_state(config_dir: Path) -> tuple[bool, str]:
    """For doctor: STATE_FILE reads, and when it holds project entries, at least one uses
    STATE_TRUST (else the tool may have renamed the field). (ok, text without paths)."""
    if not (config_dir / STATE_FILE).exists():
        return True, "no state file yet"
    try:
        projects = _read_state(config_dir / STATE_FILE).get(STATE_PROJECTS)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return False, f"state file cannot be read ({type(e).__name__})"
    if isinstance(projects, dict) and projects and not any(
            isinstance(e, dict) and STATE_TRUST in e for e in projects.values()):
        return False, f"trust field not found; {tool} may have renamed it"
    return True, "state file reads" + (", trust field found" if projects else "")


def merge_state(dest: dict, src: dict) -> tuple[dict, list[str]]:
    """`dest` plus what may be carried over from `src`: trust for the projects `src` trusts, the
    user MCP servers `dest` lacks, the onboarding keys and the Claude in Chrome consent. Every other key of `dest` stays as it
    is, and nothing else of `src` is read. Also returns what was added, for the user: counts and
    names only, never a value."""
    new, what = dict(dest), []
    projects = dict(new.get(STATE_PROJECTS) or {})
    trusted = [p for p, e in (src.get(STATE_PROJECTS) or {}).items()
               if isinstance(e, dict) and e.get(STATE_TRUST) is True
               and isinstance(projects.get(p, {}), dict)
               and projects.get(p, {}).get(STATE_TRUST) is not True]
    for p in trusted:
        projects[p] = {**projects.get(p, {}), STATE_TRUST: True}
    if trusted:
        new[STATE_PROJECTS] = projects
        what.append(f"trust for {len(trusted)} project{'s' * (len(trusted) != 1)}")
    servers = src.get(STATE_MCP)
    mine = new.get(STATE_MCP) if isinstance(new.get(STATE_MCP), dict) else {}
    added = sorted(k for k in servers if k not in mine) if isinstance(servers, dict) else []
    if added:
        new[STATE_MCP] = {**mine, **{k: servers[k] for k in added}}
        what.append(f"MCP servers {', '.join(added)}")
    for label, group in (("onboarding", STATE_ONBOARDING),
                         ("Claude in Chrome consent", STATE_CHROME)):
        keys = [k for k in group if k in src and k not in new]
        for k in keys:
            new[k] = src[k]
        if keys:
            what.append(f"{label} ({', '.join(keys)})")
    return new, what


def masked(item: str, text: str) -> str:
    """A shared item's text fit to show the user in a diff: the settings file with the values of
    the variables it sets hidden (and laid out uniformly), or a placeholder when it is not JSON.
    Other items are shown as they are."""
    if item != SETTINGS_FILE:
        return text
    try:
        d = json.loads(text)
    except ValueError:
        return "(not valid JSON: not shown)\n"
    if isinstance(d, dict) and isinstance(d.get(SETTINGS_ENV), dict):
        d = {**d, SETTINGS_ENV: {k: "(hidden)" for k in d[SETTINGS_ENV]}}
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


# --- fairshare's skill.

def skill(shared: Path) -> tuple[Path, str]:
    """Where fairshare's skill goes among the hub's shared items, and its shipped text."""
    text = (resources.files(__package__) / SKILL_SOURCE / SKILL_FILE).read_text(encoding="utf-8")
    return shared / SKILLS_DIR / SKILL_NAME / SKILL_FILE, text


def own_skill(text: str) -> bool:
    """Whether a skill file's front matter names it fairshare's."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != SKILL_FENCE:
        return False
    for line in lines[1:]:
        if line.strip() == SKILL_FENCE:
            return False
        key, sep, value = line.partition(":")
        if sep and key.strip() == SKILL_NAME_KEY:
            return value.strip().strip("\"'") == SKILL_NAME
    return False


# --- The usage hook: pure text-in, text-out. One settings file serves every account.

def capture_file(shared: Path) -> Path:
    return shared / SETTINGS_FILE


def capture_of(command: str) -> Capture | None:
    """The capture a statusLine `command` is, else None. Reads any form fairshare ever wrote: the
    installed command by path, or an older `python -I -c '<code>'` that ran this very fairshare
    (both are everything before the word `capture`), with or without the account id that came
    after `capture` before the hub."""
    try:
        t = shlex.split(command)
    except ValueError:
        return None
    i = t.index("capture") if "capture" in t else -1
    if i < 1 or not any("fairshare" in x for x in t[:i]):
        return None
    rest = t[i + 1:]
    if rest and not rest[0].startswith("-"):
        rest = rest[1:]                              # an account id: the form before the hub
    return Capture(tuple(t[:i]), rest[1] if rest[:1] == ["--then"] and len(rest) > 1 else None)


def wired(text: str | None) -> Capture | None:
    try:
        d = json.loads(text) if text and text.strip() else {}
        return capture_of(d[STATUS_LINE_KEY][HOOK_COMMAND_KEY])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def hook_of(command: str) -> tuple[tuple[str, ...], str | None] | None:
    """(runner, hook name) when a hook `command` is fairshare's `<runner> hook <name>`, else
    None; recognised as `capture_of` recognises the capture."""
    try:
        t = shlex.split(command)
    except ValueError:
        return None
    i = t.index("hook") if "hook" in t else -1
    if i < 1 or not any("fairshare" in x for x in t[:i]):
        return None
    return tuple(t[:i]), t[i + 1] if len(t) > i + 1 else None


def _own_hooks(d: dict) -> dict[str, tuple[int, int, dict, str]]:
    """fairshare's hooks found in the settings object `d`: for each of its hook names, where it
    is in its event's list (group, command) and that group and command."""
    found = {}
    hooks = d.get(HOOKS_KEY)
    for name, (event, _) in FAIRSHARE_HOOKS.items():
        groups = hooks.get(event) if isinstance(hooks, dict) else None
        for gi, g in enumerate(groups if isinstance(groups, list) else []):
            cmds = g.get(HOOK_LIST_KEY) if isinstance(g, dict) else None
            for hi, h in enumerate(cmds if isinstance(cmds, list) else []):
                cmd = h.get(HOOK_COMMAND_KEY) if isinstance(h, dict) else None
                if (name not in found and isinstance(cmd, str) and (own := hook_of(cmd))
                        and own[1] == name):
                    found[name] = (gi, hi, g, cmd)
    return found


def hooked(text: str | None) -> tuple[str, ...] | None:
    """The fairshare command that every one of fairshare's hooks in that settings text runs, with
    its matcher (where its event takes one); None when one is missing (or they run different
    commands)."""
    try:
        d = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return None
    found = _own_hooks(d) if isinstance(d, dict) else {}
    matched = [hook_of(cmd)[0] for name, (_, _, g, cmd) in found.items()
               if FAIRSHARE_HOOKS[name][1] is None
               or g.get(HOOK_MATCHER_KEY) == FAIRSHARE_HOOKS[name][1]]
    runners = set(matched)
    return runners.pop() if len(matched) == len(FAIRSHARE_HOOKS) and len(runners) == 1 else None


def remote_controlled(text: str | None) -> bool | None:
    """Whether that settings text starts remote control in every session: True or False as it
    says; None when it says nothing (or cannot be read)."""
    try:
        d = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return None
    v = d.get(REMOTE_CONTROL_KEY) if isinstance(d, dict) else None
    return v if isinstance(v, bool) else None


def with_remote_control(text: str | None) -> str | None:
    """That settings text with remote control started in every session; None when it already
    says either way (the user's own answer stays). Every other byte stays. Raises ValueError for
    a text that is not a settings object."""
    d = json.loads(text) if text and text.strip() else {}
    if not isinstance(d, dict):
        raise ValueError("unexpected layout")
    if isinstance(d.get(REMOTE_CONTROL_KEY), bool):
        return None
    if not (text and text.strip()):
        return json.dumps({REMOTE_CONTROL_KEY: True}, indent=2) + "\n"
    new = _json_set(text, [REMOTE_CONTROL_KEY], True)
    if json.loads(new) != {**d, REMOTE_CONTROL_KEY: True}:
        raise ValueError("could not change that setting alone")
    return new


def wire(text: str | None, command: str) -> str | None:
    """Settings text whose statusLine runs `<command> capture`, after which it runs the user's
    own statusLine (`--then`), re-run every STATUS_REFRESH_S seconds unless it already names an
    interval, and which holds fairshare's hooks (`<command> hook <name>` under each one's event
    and matcher); None when it already does all that. An older capture is replaced by this one
    and keeps the user's command it wrapped; an older hook of fairshare's is changed in place.
    A missing hook is added at the end of its event's list: no other hook is changed, moved or
    removed, and every byte outside what changes stays. Raises ValueError for a text that is not
    a settings object of the expected layout."""
    d = json.loads(text) if text and text.strip() else {}
    if not isinstance(d, dict) or not isinstance(d.get(STATUS_LINE_KEY, {}), dict) \
            or not isinstance(d.get(HOOKS_KEY, {}), dict) \
            or any(not isinstance(d.get(HOOKS_KEY, {}).get(ev, []), list)
                   for ev, _ in FAIRSHARE_HOOKS.values()):
        raise ValueError("unexpected layout")
    sl = d.get(STATUS_LINE_KEY) or {}
    theirs = sl.get(HOOK_COMMAND_KEY) if isinstance(sl.get(HOOK_COMMAND_KEY), str) else None
    if theirs and (cap := capture_of(theirs)):
        theirs = cap.then
    cmd = f"{command} capture" + (f" --then {shlex.quote(theirs)}" if theirs else "")
    line = {HOOK_TYPE_KEY: HOOK_TYPE, HOOK_COMMAND_KEY: cmd}
    empty = not (text and text.strip())
    if empty:
        text = new = "{}"
        new = _json_set(new, [STATUS_LINE_KEY], {**line, STATUS_REFRESH_KEY: STATUS_REFRESH_S})
    elif STATUS_LINE_KEY not in d:
        new = _json_set(text, [STATUS_LINE_KEY], {**line, STATUS_REFRESH_KEY: STATUS_REFRESH_S})
    else:
        new = text
        if sl.get(HOOK_TYPE_KEY) != HOOK_TYPE:
            new = _json_set(new, [STATUS_LINE_KEY, HOOK_TYPE_KEY], HOOK_TYPE)
        if sl.get(HOOK_COMMAND_KEY) != cmd:
            new = _json_set(new, [STATUS_LINE_KEY, HOOK_COMMAND_KEY], cmd)
        if STATUS_REFRESH_KEY not in sl:
            new = _json_set(new, [STATUS_LINE_KEY, STATUS_REFRESH_KEY], STATUS_REFRESH_S)
    want = {**d, STATUS_LINE_KEY: {**sl, **line}}
    want[STATUS_LINE_KEY].setdefault(STATUS_REFRESH_KEY, STATUS_REFRESH_S)
    found = _own_hooks(json.loads(new))
    hooks = dict(want.get(HOOKS_KEY) or {})
    for name, (event, matcher) in FAIRSHARE_HOOKS.items():
        hook_cmd = f"{command} hook {name}"
        groups = list(hooks.get(event) or [])
        if name in found:
            gi, hi, g, old = found[name]
            if old != hook_cmd:
                new = _json_set(new, [HOOKS_KEY, event, gi, HOOK_LIST_KEY, hi, HOOK_COMMAND_KEY],
                                hook_cmd)
            if matcher is not None and g.get(HOOK_MATCHER_KEY) != matcher:
                new = _json_set(new, [HOOKS_KEY, event, gi, HOOK_MATCHER_KEY], matcher)
            cmds = list(g[HOOK_LIST_KEY])
            cmds[hi] = {**cmds[hi], HOOK_COMMAND_KEY: hook_cmd}
            groups[gi] = {**g, **({HOOK_MATCHER_KEY: matcher} if matcher is not None else {}),
                          HOOK_LIST_KEY: cmds}
        else:
            group = {**({HOOK_MATCHER_KEY: matcher} if matcher is not None else {}),
                     HOOK_LIST_KEY: [{HOOK_TYPE_KEY: HOOK_TYPE, HOOK_COMMAND_KEY: hook_cmd}]}
            if event in hooks:
                new = _json_append(new, [HOOKS_KEY, event], group)
            else:
                new = _json_set(new, [HOOKS_KEY, event], [group])
            groups.append(group)
        hooks[event] = groups
    want[HOOKS_KEY] = hooks
    if empty:
        return json.dumps(want, indent=2) + "\n"
    if new == text:
        return None
    if json.loads(new) != want:
        raise ValueError("could not change fairshare's own entries alone")
    return new


def repath(text: str, old: str, new: str) -> tuple[str | None, list[str]]:
    """Settings text in which every string value naming the folder `old` (the path itself or
    something inside it) names `new` instead, and where each was (key paths, never values).
    Keys and every other byte stay. (None, []) when nothing names it. Raises ValueError for a
    text it cannot change safely."""
    d, places = json.loads(text), []
    after = r"(?![\w.-])"                   # /x/hub matches /x/hub and /x/hub/a, not /x/hub2

    def walk(v, where: str):
        if isinstance(v, str):
            if re.search(re.escape(old) + after, v):
                places.append(where)
                return re.sub(re.escape(old) + after, lambda m: new, v)
            return v
        if isinstance(v, dict):
            return {k: walk(x, f"{where}.{k}" if where else k) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{where}[{i}]") for i, x in enumerate(v)]
        return v
    want = walk(d, "")
    if not places:
        return None, []
    esc_old, esc_new = (json.dumps(x, ensure_ascii=False)[1:-1] for x in (old, new))
    out = re.sub(re.escape(esc_old) + after, lambda m: esc_new, text)
    if json.loads(out) != want:
        raise ValueError("the old path is not only in string values")
    return out, places


_WS = re.compile(r"\s*")
_DECODER = json.JSONDecoder()


def _members(text: str, start: int) -> list[tuple[str, int, int, int]]:
    """(key, key start, value start, value end) of each member of the JSON object at `start`."""
    if text[start:start + 1] != "{":
        raise ValueError("not an object")
    out, i = [], _WS.match(text, start + 1).end()
    if text[i:i + 1] == "}":
        return out
    while True:
        k0 = i
        if text[i:i + 1] != '"':
            raise ValueError("bad object")
        key, i = json.decoder.scanstring(text, i + 1)
        i = _WS.match(text, i).end()
        if text[i:i + 1] != ":":
            raise ValueError("bad object")
        v0 = _WS.match(text, i + 1).end()
        _, v1 = _DECODER.raw_decode(text, v0)
        out.append((key, k0, v0, v1))
        i = _WS.match(text, v1).end()
        if text[i:i + 1] == "}":
            return out
        if text[i:i + 1] != ",":
            raise ValueError("bad object")
        i = _WS.match(text, i + 1).end()


def _elements(text: str, start: int) -> list[tuple[int, int]]:
    """(value start, value end) of each element of the JSON array at `start`."""
    if text[start:start + 1] != "[":
        raise ValueError("not an array")
    out, i = [], _WS.match(text, start + 1).end()
    if text[i:i + 1] == "]":
        return out
    while True:
        _, end = _DECODER.raw_decode(text, i)
        out.append((i, end))
        i = _WS.match(text, end).end()
        if text[i:i + 1] == "]":
            return out
        if text[i:i + 1] != ",":
            raise ValueError("bad array")
        i = _WS.match(text, i + 1).end()


def _json_set(text: str, keys: list, value) -> str:
    """`text` with the value at `keys` (member names, or indexes of array elements that exist)
    replaced, or added as the last member of the innermost object that exists, laid out like its
    first member; every other byte stays."""
    start = _WS.match(text).end()
    for depth, key in enumerate(keys):
        if isinstance(key, int):
            v0, v1 = _elements(text, start)[key]
        else:
            members = _members(text, start)
            hit = [m for m in members if m[0] == key]
            if not hit:
                for k in reversed(keys[depth + 1:]):
                    value = {k: value}
                member = f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
                if not members:
                    return text[:start + 1] + member + text[start + 1:]
                lead = text[start + 1:members[0][1]] or " "
                return text[:members[-1][3]] + "," + lead + member + text[members[-1][3]:]
            _, _, v0, v1 = hit[-1]                   # the last of duplicate keys is the one read
        if depth == len(keys) - 1:
            return text[:v0] + json.dumps(value, ensure_ascii=False) + text[v1:]
        start = v0
    return text


def _json_append(text: str, keys: list, value) -> str:
    """`text` with `value` added as the last element of the array at `keys` (which exists), laid
    out like its first element; every other byte stays."""
    start = _WS.match(text).end()
    for key in keys:
        if isinstance(key, int):
            start = _elements(text, start)[key][0]
        else:
            start = [m for m in _members(text, start) if m[0] == key][-1][2]
    items, item = _elements(text, start), json.dumps(value, ensure_ascii=False)
    if not items:
        return text[:start + 1] + item + text[start + 1:]
    lead = text[start + 1:items[0][0]]
    return text[:items[-1][1]] + "," + lead + item + text[items[-1][1]:]
