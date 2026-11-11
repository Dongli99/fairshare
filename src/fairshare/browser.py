"""Which browser an account's sign-in link opens in. Provider-neutral: the provider's own sign-in
calls the program named by BROWSER with the link as its one argument (a value with arguments is
not split, so it must name one executable). fairshare writes a one-line launcher per account and
names it there; it never sees the link.

An account's `browser` line in accounts.toml:
    absent                     the system default
    chrome:<profile folder>    that Google Chrome profile (macOS)
    command:<absolute path>    an executable taking the link

Chrome's profiles are read from its own `Local State` file, and only each profile's folder name
and display name; no other browser data is read."""
from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path

ENV = "BROWSER"
CHROME, COMMAND = "chrome:", "command:"
CHROME_APP = "Google Chrome"
CHROME_LOCAL_STATE = "Library/Application Support/Google/Chrome/Local State"   # under HOME
CHROME_PROFILES = ("profile", "info_cache")          # where Local State lists the profiles
CHROME_NAME = "name"                                 # a profile's display name


def parse(spec: str) -> tuple[str, str]:
    """(kind, value) of a `browser` line; raises ValueError for one that is not accepted."""
    if isinstance(spec, str) and spec.startswith(CHROME) and spec[len(CHROME):].strip():
        return CHROME, spec[len(CHROME):]
    if isinstance(spec, str) and spec.startswith(COMMAND) and os.path.isabs(spec[len(COMMAND):]):
        return COMMAND, spec[len(COMMAND):]
    raise ValueError(f"browser {spec!r}: use '{CHROME}<profile folder>' or "
                     f"'{COMMAND}<absolute path>' (or leave it out for the default browser)")


def chrome_profiles(home: Path) -> list[tuple[str, str]]:
    """(profile folder, display name) of each Chrome profile, by display name; [] when Chrome's
    Local State is missing or unreadable."""
    try:
        d = json.loads((home / CHROME_LOCAL_STATE).read_text(encoding="utf-8"))
        for key in CHROME_PROFILES:
            d = d[key]
        found = [(folder, str(p.get(CHROME_NAME) or folder)) for folder, p in d.items()
                 if isinstance(p, dict)]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        return []
    return sorted(found, key=lambda x: (x[1].lower(), x[0]))


def script(spec: str) -> str:
    """The launcher's text: a shell script that opens its one argument as `spec` says."""
    kind, value = parse(spec)
    if kind == CHROME:
        line = (f"exec open -na {shlex.quote(CHROME_APP)} --args "
                f"--profile-directory={shlex.quote(value)} \"$1\"")
    else:
        line = f"exec {shlex.quote(value)} \"$1\""
    return f"#!/bin/sh\n{line}\n"


def launcher(hub: Path, account_id: str) -> Path:
    return hub / "browsers" / account_id


def ensure(hub: Path, account_id: str, spec: str | None) -> Path | None:
    """The account's launcher, (re)written when missing or different, mode 755; None when the
    account uses the default browser. Raises OSError when it cannot be written."""
    if spec is None:
        return None
    path, text = launcher(hub, account_id), script(spec)
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = None
    if current != text or not os.access(path, os.X_OK):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    return path


def env(hub: Path, account_id: str, spec: str | None) -> dict[str, str]:
    """The variable to add to the account's sign-in and sessions: {} for the default browser."""
    path = ensure(hub, account_id, spec)
    return {ENV: str(path.absolute())} if path else {}
