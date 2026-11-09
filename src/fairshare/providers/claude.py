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


def shim_script(python: str, pkg_parent: str) -> str:
    """A POSIX `claude` that routes through `fairshare run`, or, when CLAUDE_CONFIG_DIR is
    already set (an account alias), runs the real claude untouched."""
    code = (f"import sys; sys.path.insert(0, {json.dumps(pkg_parent)}); "
            'from fairshare.cli import main; sys.exit(main(["run", "--", *sys.argv[1:]]))')
    return f"""#!/bin/sh
# {SHIM_MARK}: written by `fairshare shim install`. Delete it to stop routing.
# Plain `claude` routes through fairshare. With CLAUDE_CONFIG_DIR set, the real claude runs as is.
if [ -z "${{CLAUDE_CONFIG_DIR:-}}" ]; then
  exec {shlex.quote(python)} -I -c {shlex.quote(code)} "$@"
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
