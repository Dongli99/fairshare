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
VERSION_ARGS = ("--version",)
PROBE_TIMEOUT = 5.0                          # seconds

# Environment variables.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"         # picks the account's folder
LOGIN_OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
SESSION_ENV = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")   # state of a surrounding session
SESSION_ENV_PREFIX = "CLAUDE_CODE_"

# Files and folders.
LOGIN_FILE = ".credentials.json"     # its presence marks a signed-in dir; never opened
SETTINGS_FILE = "settings.json"      # holds the statusLine hook
RC_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".bash_aliases",
            ".profile")              # read-only, for aliases that set CONFIG_DIR_ENV
_ALIAS = re.compile(r"""^\s*alias\s+([\w.-]+)=["']?\s*(?:env\s+)?"""
                    + re.escape(CONFIG_DIR_ENV) + r"""=("[^"]*"|'[^']*'|[^\s"';]+)""")

# JSON fields: the statusLine entry in the settings file, and the usage payload it is given.
STATUS_LINE_KEY = "statusLine"
HOOK_TYPE_KEY, HOOK_TYPE = "type", "command"
HOOK_COMMAND_KEY = "command"
CAPTURE_LIMITS, CAPTURE_USED, CAPTURE_RESET = "rate_limits", "used_percentage", "resets_at"

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


def _scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in LOGIN_OVERRIDE_ENV and k not in SESSION_ENV
            and not k.startswith(SESSION_ENV_PREFIX)}


def clean_env(config_dir: Path) -> dict[str, str]:
    """The environment for a child claude of one account: this process's, minus every variable
    that would pick another login or carry another session's state, with the account's dir."""
    return {**_scrubbed_env(), CONFIG_DIR_ENV: str(config_dir)}


def dropped_login_vars() -> list[str]:
    return [k for k in LOGIN_OVERRIDE_ENV if k in os.environ]


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
    memory and only its yes/no, a hash and a plan name survive. None on any failure."""
    try:
        r = subprocess.run([real_binary(), *(account.status_args or STATUS_ARGS)],
                           env=clean_env(account.config_dir), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        return parse_account_status(json.loads(r.stdout)) if r.returncode == 0 else None
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
    """Accounts set up under `home`: aliases of the form `alias X="CONFIG_DIR_ENV=P ..."` in
    shell rc files (best effort), and ~/.claude* dirs holding a login file. Reads no login."""
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
    for d in sorted(home.glob(".claude*")):
        if d not in found and d.is_dir() and (d / LOGIN_FILE).is_file():
            found[d] = Candidate(d.name.lstrip("."), name, d, f"login file in ~/{d.name}")
    return sorted(found.values(), key=lambda c: c.id)


def _expand(p: str, home: Path) -> Path:
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if p.startswith(prefix):
            return home / p[len(prefix):]
    return home if p in ("~", "$HOME", "${HOME}") else Path(p)


# --- statusLine wiring: pure text-in, text-out. The CLI reads the files, shows diffs, asks, writes.

def capture_file(account: Account) -> Path:
    return account.config_dir / SETTINGS_FILE


def wire(files: dict[Path, str | None], accounts: list[Account],
         command: str) -> dict[Path, str]:
    """New text for each settings file that must change so that every account records its usage
    through `command` (a shell command that runs fairshare)."""
    out = {}
    for a in accounts:
        p = capture_file(a)
        out[p] = _settings(files.get(p), p, lambda sl: _wrap(sl, a.id, command))
    return {p: t for p, t in out.items() if t is not None}


def unwire(files: dict[Path, str | None], accounts: list[Account]) -> dict[Path, str]:
    """The reverse of wire(): new text for each file that carries fairshare's changes."""
    out = {}
    for a in accounts:
        p = capture_file(a)
        if files.get(p) is not None:
            out[p] = _settings(files[p], p, _unwrap)
    return {p: t for p, t in out.items() if t is not None}


def capture_of(command: str) -> Capture | None:
    """The capture a statusLine `command` is, else None. Reads any form fairshare ever wrote:
    the installed command by path, or an older `python -I -c '<code>'` that ran this very
    fairshare; both are everything before the word `capture`."""
    try:
        t = shlex.split(command)
    except ValueError:
        return None
    i = t.index("capture") if "capture" in t else -1
    if i < 1 or i + 1 >= len(t) or not any("fairshare" in x for x in t[:i]):
        return None
    rest = t[i + 2:]
    return Capture(t[i + 1], tuple(t[:i]),
                   rest[1] if rest[:1] == ["--then"] and len(rest) > 1 else None)


def wired(text: str | None, account_id: str) -> Capture | None:
    try:
        d = json.loads(text) if text and text.strip() else {}
        cap = capture_of(d[STATUS_LINE_KEY][HOOK_COMMAND_KEY])
    except (ValueError, KeyError, TypeError):
        return None
    return cap if cap and cap.account == account_id else None


def _wrap(sl: dict | None, account_id: str, fairshare: str) -> dict | None:
    theirs = (sl or {}).get(HOOK_COMMAND_KEY)
    if theirs and capture_of(theirs):
        return sl                                   # already records usage
    cmd = f"{fairshare} capture {shlex.quote(account_id)}"
    if theirs:
        cmd += f" --then {shlex.quote(theirs)}"     # never overwrite: run theirs after recording
    return {**(sl or {}), HOOK_TYPE_KEY: HOOK_TYPE, HOOK_COMMAND_KEY: cmd}


def _unwrap(sl: dict | None) -> dict | None:
    cap = capture_of((sl or {}).get(HOOK_COMMAND_KEY) or "")
    if cap is None:
        return sl
    return None if cap.then is None else {**sl, HOOK_COMMAND_KEY: cap.then}


def _settings(text: str | None, path: Path, change) -> str | None:
    """Apply `change` to the statusLine of a settings.json text; None when nothing changes."""
    d = json.loads(text) if text and text.strip() else {}
    if not isinstance(d, dict) or not isinstance(d.get(STATUS_LINE_KEY, {}), dict):
        raise ValueError(f"{path}: unexpected layout, left alone")
    old = d.get(STATUS_LINE_KEY)
    new = change(old)
    if new == old:
        return None
    if new is None:
        d.pop(STATUS_LINE_KEY)
    else:
        d[STATUS_LINE_KEY] = new
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
