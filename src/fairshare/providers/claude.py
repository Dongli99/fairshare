"""Claude Code. Usage arrives on the statusLine command's stdin (documented), one account per
CLAUDE_CONFIG_DIR. fairshare never reads or copies a login."""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from ..model import Account, Candidate, Window

name = "claude"
default_plan = "claude-pro"
LOGIN_FILE = ".credentials.json"     # its presence marks a signed-in dir; never opened
RC_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".bash_aliases",
            ".profile")
_ALIAS = re.compile(r"""^\s*alias\s+([\w.-]+)=["']?\s*(?:env\s+)?"""
                    r"""CLAUDE_CONFIG_DIR=("[^"]*"|'[^']*'|[^\s"';]+)""")
SHIM_MARK = "fairshare-shim"     # in the first bytes of every shim; the lookup skips such files


def parse_capture(raw: dict) -> dict[str, Window]:
    out = {}
    for key, w in (raw.get("rate_limits") or {}).items():
        if key in ("five_hour", "seven_day") and isinstance(w, dict) \
                and "used_percentage" in w and "resets_at" in w:
            out[key] = Window(float(w["used_percentage"]), float(w["resets_at"]))
    return out


def launch(account: Account) -> tuple[list[str], dict[str, str]]:
    return [real_binary()], {"CLAUDE_CONFIG_DIR": str(account.config_dir)}


def discover(home: Path) -> list[Candidate]:
    """Accounts set up under `home`: aliases of the form `alias X="CLAUDE_CONFIG_DIR=P ..."` in
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


def is_shim(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return SHIM_MARK.encode() in f.read(64)
    except OSError:
        return False


def real_binary(path: str | None = None) -> str:
    """The first `claude` on PATH that is not a fairshare shim. The shim runs the same walk."""
    for d in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep):
        f = os.path.join(d, "claude")
        if d and os.path.isfile(f) and os.access(f, os.X_OK) and not is_shim(f):
            return f
    raise SystemExit("fairshare: no claude found on PATH besides the fairshare shim")


def shim_script(fairshare: str) -> str:
    """A POSIX `claude` that routes through `fairshare run`, or, when CLAUDE_CONFIG_DIR is
    already set (an account alias), runs the real claude untouched. `fairshare` is the shell
    command that runs fairshare."""
    return f"""#!/bin/sh
# {SHIM_MARK}: written by `fairshare shim install`. Delete it to stop routing.
# Plain `claude` routes through fairshare. With CLAUDE_CONFIG_DIR set, the real claude runs as is.
if [ -z "${{CLAUDE_CONFIG_DIR:-}}" ]; then
  exec {fairshare} run -- "$@"
fi
set -f
IFS=:
for d in $PATH; do
  f="$d/claude"
  if [ -n "$d" ] && [ -f "$f" ] && [ -x "$f" ] && ! head -c 64 "$f" | grep -q {SHIM_MARK}; then
    exec "$f" "$@"
  fi
done
echo "fairshare: no claude found on PATH besides the fairshare shim" >&2
exit 127
"""


# --- Wiring: pure text-in, text-out. The CLI reads the files, shows diffs, asks, writes.

BLOCK_BEGIN = "# >>> fairshare >>>"
BLOCK_END = "# <<< fairshare <<<"
DISABLED = "# fairshare-disabled: "     # prefix on a user's `alias claude=` line; uninstall strips it
_CLAUDE_ALIAS = re.compile(r"\s*alias\s+claude=")


def rc_file(shell: str) -> str:
    """The rc file that gets the PATH block, from the login shell's path."""
    return {"zsh": ".zshrc", "bash": ".bashrc"}.get(os.path.basename(shell), ".profile")


def wire(files: dict[Path, str | None], accounts: list[Account], home: Path, shell: str,
         fairshare: str, shim_dir: Path) -> dict[Path, str]:
    """New text for each file that must change so that every account records its usage and
    plain `claude` finds the shim first. `files` maps path -> current text (None: absent)."""
    out = {}
    for a in accounts:
        p = a.config_dir / "settings.json"
        out[p] = _settings(files.get(p), p, lambda sl: _wrap(sl, a.id, fairshare))
    shown = f"$HOME/{shim_dir.relative_to(home)}" if shim_dir.is_relative_to(home) else str(shim_dir)
    block = [f"{BLOCK_BEGIN} added by `fairshare apply`; `fairshare uninstall` removes it",
             f'export PATH="{shown}:$PATH"', BLOCK_END]
    target = home / rc_file(shell)
    for p in [home / rc for rc in RC_FILES]:
        lines = _strip_block((files.get(p) or "").splitlines())
        lines = [DISABLED + x if _CLAUDE_ALIAS.match(x) else x for x in lines]
        out[p] = _lines(files.get(p), lines + block if p == target else lines)
    return {p: t for p, t in out.items() if t is not None}


def unwire(files: dict[Path, str | None], accounts: list[Account], home: Path) -> dict[Path, str]:
    """The reverse of wire(): new text for each file that carries fairshare's changes."""
    out = {}
    for a in accounts:
        p = a.config_dir / "settings.json"
        if files.get(p) is not None:
            out[p] = _settings(files[p], p, _unwrap)
    for p in [home / rc for rc in RC_FILES]:
        lines = _strip_block((files.get(p) or "").splitlines())
        out[p] = _lines(files.get(p), [x.removeprefix(DISABLED) for x in lines])
    return {p: t for p, t in out.items() if t is not None}


def capture_of(command: str) -> tuple[str, str | None] | None:
    """(account id, wrapped command or None) if `command` is a fairshare capture, else None."""
    try:
        t = shlex.split(command)
    except ValueError:
        return None
    i = t.index("capture") if "capture" in t else -1
    if i < 1 or i + 1 >= len(t) or not any("fairshare" in x for x in t[:i]):
        return None
    rest = t[i + 2:]
    return t[i + 1], rest[1] if rest[:1] == ["--then"] and len(rest) > 1 else None


def _wrap(sl: dict | None, account_id: str, fairshare: str) -> dict | None:
    theirs = (sl or {}).get("command")
    if theirs and capture_of(theirs):
        return sl                                   # already records usage
    cmd = f"{fairshare} capture {shlex.quote(account_id)}"
    if theirs:
        cmd += f" --then {shlex.quote(theirs)}"     # never overwrite: run theirs after recording
    return {**(sl or {}), "type": "command", "command": cmd}


def _unwrap(sl: dict | None) -> dict | None:
    cap = capture_of((sl or {}).get("command") or "")
    if cap is None:
        return sl
    return None if cap[1] is None else {**sl, "command": cap[1]}


def _settings(text: str | None, path: Path, change) -> str | None:
    """Apply `change` to the statusLine of a settings.json text; None when nothing changes."""
    d = json.loads(text) if text and text.strip() else {}
    if not isinstance(d, dict) or not isinstance(d.get("statusLine", {}), dict):
        raise ValueError(f"{path}: unexpected layout, left alone")
    old = d.get("statusLine")
    new = change(old)
    if new == old:
        return None
    if new is None:
        d.pop("statusLine")
    else:
        d["statusLine"] = new
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


def _strip_block(lines: list[str]) -> list[str]:
    """Drop complete fairshare blocks; a block missing its end line is left as the user has it."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].startswith(BLOCK_BEGIN) and BLOCK_END in lines[i:]:
            i = lines.index(BLOCK_END, i) + 1
        else:
            out.append(lines[i])
            i += 1
    return out


def _lines(text: str | None, lines: list[str]) -> str | None:
    """New file text, or None when the lines are unchanged."""
    if lines == (text or "").splitlines():
        return None
    return "\n".join(lines) + "\n" if lines else ""
