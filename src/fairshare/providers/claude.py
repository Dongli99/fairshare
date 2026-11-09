"""Claude Code. Usage arrives on the statusLine command's stdin (documented), one account per
CLAUDE_CONFIG_DIR. fairshare never reads or copies a login."""
from __future__ import annotations

from ..model import Account, Window

name = "claude"


def parse_capture(raw: dict) -> dict[str, Window]:
    out = {}
    for key, w in (raw.get("rate_limits") or {}).items():
        if key in ("five_hour", "seven_day") and isinstance(w, dict) \
                and "used_percentage" in w and "resets_at" in w:
            out[key] = Window(float(w["used_percentage"]), float(w["resets_at"]))
    return out


def launch(account: Account) -> tuple[list[str], dict[str, str]]:
    return ["claude"], {"CLAUDE_CONFIG_DIR": str(account.config_dir)}
