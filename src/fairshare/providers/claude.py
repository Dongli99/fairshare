"""Claude Code. Usage arrives on the statusLine command's stdin (documented), one account per
CLAUDE_CONFIG_DIR. fairshare never reads or copies a login.

Everything Anthropic may rename lives in the constants below and nowhere else in fairshare."""
from __future__ import annotations

import json
import os
import hashlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from ..model import (Account, AccountStatus, Candidate, Capture, Window, plan_for,
                     window_names)

name = "claude"                      # fairshare's own id for this provider (accounts.toml, plans)
default_plan = "claude-pro"

# The command-line tool.
BINARY = "claude"
STATUS_ARGS = ("auth", "status", "--json")   # the sign-in check; accounts.toml may override it
LOGIN_ARGS = ("auth", "login")               # interactive: the tool owns the browser sign-in
LOGOUT_ARGS = ("auth", "logout")
VERSION_ARGS = ("--version",)
PROBE_TIMEOUT = 5.0                          # seconds

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

# Files and folders.
LOGIN_FILE = ".credentials.json"     # its presence marks a signed-in dir; never opened
SETTINGS_FILE = "settings.json"      # holds the statusLine hook
# What every account shares through the hub; a name ending in "/" is a folder. Everything else in
# an account's folder (its login, STATE_FILE, caches, jobs, locks) stays its own. Observed in real
# config dirs on 2.1.322; CLAUDE.md, agents/, commands/, output-styles/ and keybindings.json are
# documented as user-level files (code.claude.com/docs/en/claude-directory, 2026-11-09).
SHARED = ("projects/", "history.jsonl", "file-history/", "paste-cache/", "plugins/", "skills/",
          "tasks/", SETTINGS_FILE, "sessions/", "CLAUDE.md", "agents/", "commands/",
          "output-styles/", "keybindings.json")
shared = SHARED
# Shared, but never copied in by join: the sessions running now, each with its own key. A copy
# would be stale on arrival and would carry another session's key.
NOT_COPIED = ("sessions/",)
not_copied = NOT_COPIED
STATE_FILE = ".claude.json"          # the tool's own state, inside CONFIG_DIR_ENV's folder
STATE_LOCK = STATE_FILE + ".lock"    # the folder the tool holds while it writes STATE_FILE
DEFAULT_DIR = ".claude"              # the folder used without CONFIG_DIR_ENV; its STATE_FILE is
                                     # beside it in HOME
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

# JSON fields of STATE_FILE that join may carry over from a user's folder, and nothing else (never
# identity, tokens or account fields). Onboarding is marked done by these two keys, set together
# when the first-run screens finish (2.1.322); the theme chosen there goes to SETTINGS_FILE.
STATE_PROJECTS, STATE_TRUST = "projects", "hasTrustDialogAccepted"
STATE_MCP = "mcpServers"
STATE_ONBOARDING = ("hasCompletedOnboarding", "lastOnboardingVersion")

# JSON fields and values of the sign-in check's output.
STATUS_LOGGED_IN, STATUS_METHOD = "loggedIn", "authMethod"
STATUS_SUBSCRIPTION = "claude.ai"            # the STATUS_METHOD of a subscription login
STATUS_ORG, STATUS_EMAIL, STATUS_PLAN = "orgId", "email", "planTier"


def parse_capture(raw: dict) -> dict[str, Window]:
    known = window_names(name)               # the plan files say which windows exist
    out = {}
    for key, w in (raw.get(CAPTURE_LIMITS) or {}).items():
        if key in known and isinstance(w, dict) and CAPTURE_USED in w and CAPTURE_RESET in w:
            out[key] = Window(float(w[CAPTURE_USED]), float(w[CAPTURE_RESET]))
    return out


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


def parse_account_status(raw: object) -> AccountStatus | None:
    """The status JSON as an AccountStatus; None when it is not the shape we know."""
    if not isinstance(raw, dict) or not isinstance(raw.get(STATUS_LOGGED_IN), bool):
        return None
    if not raw[STATUS_LOGGED_IN]:
        return AccountStatus(False, False)
    org, email = raw.get(STATUS_ORG), raw.get(STATUS_EMAIL)
    who = hashlib.sha256(f"{org}\n{email}".encode()).hexdigest() if org and email else None
    return AccountStatus(True, raw.get(STATUS_METHOD) == STATUS_SUBSCRIPTION, who,
                         plan_for(name, raw.get(STATUS_PLAN)))


def account_status(account: Account) -> AccountStatus | None:
    """Ask the installed claude who `account` is. Reads no login file; the output is parsed in
    memory and only its yes/no, a hash and a plan name survive. None on any failure. The exit
    code is not read: a signed-out folder prints its answer and exits 1 (2.1.322)."""
    try:
        r = subprocess.run([real_binary(), *(account.status_args or STATUS_ARGS)],
                           env=clean_env(account.config_dir), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        return parse_account_status(json.loads(r.stdout))
    except (SystemExit, OSError, subprocess.SubprocessError, ValueError):
        return None


def login_present(account: Account) -> bool:
    return (account.config_dir / LOGIN_FILE).exists()


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
    shell rc files (best effort), and ACCOUNT_DIR_GLOB dirs holding a login file or anything on
    the share list, signed in or not. Reads no login."""
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
        if (d / LOGIN_FILE).is_file():
            found[d] = Candidate(d.name.lstrip("."), name, d, f"login file in ~/{d.name}")
        elif any(os.path.lexists(d / item.rstrip("/")) for item in SHARED):
            found[d] = Candidate(d.name.lstrip("."), name, d, f"folder ~/{d.name}")
    return sorted(found.values(), key=lambda c: c.id)


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


def merge_state(dest: dict, src: dict) -> tuple[dict, list[str]]:
    """`dest` plus what may be carried over from `src`: trust for the projects `src` trusts, the
    user MCP servers `dest` lacks, and the onboarding keys. Every other key of `dest` stays as it
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
    keys = [k for k in STATE_ONBOARDING if k in src and k not in new]
    for k in keys:
        new[k] = src[k]
    if keys:
        what.append(f"onboarding ({', '.join(keys)})")
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


def wire(text: str | None, command: str) -> str | None:
    """Settings text whose statusLine runs `<command> capture`, after which it runs the user's
    own statusLine (`--then`); None when it already does. An older capture is replaced by this
    one and keeps the user's command it wrapped. Only the statusLine's command (and its type,
    when that is not a command) change: every other byte of `text` stays. Raises ValueError for
    a text that is not a settings object."""
    d = json.loads(text) if text and text.strip() else {}
    if not isinstance(d, dict) or not isinstance(d.get(STATUS_LINE_KEY, {}), dict):
        raise ValueError("unexpected layout")
    sl = d.get(STATUS_LINE_KEY) or {}
    theirs = sl.get(HOOK_COMMAND_KEY) if isinstance(sl.get(HOOK_COMMAND_KEY), str) else None
    if theirs and (cap := capture_of(theirs)):
        theirs = cap.then
    cmd = f"{command} capture" + (f" --then {shlex.quote(theirs)}" if theirs else "")
    if sl.get(HOOK_COMMAND_KEY) == cmd and sl.get(HOOK_TYPE_KEY) == HOOK_TYPE:
        return None
    hook = {HOOK_TYPE_KEY: HOOK_TYPE, HOOK_COMMAND_KEY: cmd}
    if not (text and text.strip()):
        return json.dumps({STATUS_LINE_KEY: hook}, indent=2) + "\n"
    if STATUS_LINE_KEY not in d:
        new = _json_set(text, [STATUS_LINE_KEY], hook)
    else:
        new = text
        if sl.get(HOOK_TYPE_KEY) != HOOK_TYPE:
            new = _json_set(new, [STATUS_LINE_KEY, HOOK_TYPE_KEY], HOOK_TYPE)
        new = _json_set(new, [STATUS_LINE_KEY, HOOK_COMMAND_KEY], cmd)
    want = {**d, STATUS_LINE_KEY: {**sl, HOOK_TYPE_KEY: HOOK_TYPE, HOOK_COMMAND_KEY: cmd}}
    if json.loads(new) != want:
        raise ValueError("could not change the statusLine alone")
    return new


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


def _json_set(text: str, keys: list[str], value) -> str:
    """`text` with the value at `keys` replaced, or added as the first member of the innermost
    object that exists; every other byte stays."""
    start = _WS.match(text).end()
    for depth, key in enumerate(keys):
        members = _members(text, start)
        hit = [m for m in members if m[0] == key]
        if not hit:
            for k in reversed(keys[depth + 1:]):
                value = {k: value}
            member = f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
            if not members:
                return text[:start + 1] + member + text[start + 1:]
            lead = text[start + 1:members[0][1]]
            return text[:members[0][1]] + member + "," + lead + text[members[0][1]:]
        _, _, v0, v1 = hit[-1]                       # the last of duplicate keys is the one read
        if depth == len(keys) - 1:
            return text[:v0] + json.dumps(value, ensure_ascii=False) + text[v1:]
        start = v0
    return text
