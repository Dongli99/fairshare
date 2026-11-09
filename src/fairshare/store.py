"""Where state lives. Snapshots are plain JSON, one per account, written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .model import Snapshot, Window


def config_path() -> Path:
    return Path(os.environ.get("FAIRSHARE_CONFIG",
                               Path.home() / ".config/fairshare/accounts.toml"))


def state_dir() -> Path:
    return Path(os.environ.get("FAIRSHARE_STATE", Path.home() / ".local/state/fairshare"))


def load(account_id: str) -> Snapshot | None:
    try:
        d = json.loads((state_dir() / "snapshots" / f"{account_id}.json").read_text())
        return Snapshot(d["captured_at"], {k: Window(**v) for k, v in d["windows"].items()})
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(account_id: str, snap: Snapshot) -> None:
    dest = state_dir() / "snapshots"
    dest.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"captured_at": snap.captured_at,
                       "windows": {k: vars(v) for k, v in snap.windows.items()}})
    fd, tmp = tempfile.mkstemp(dir=dest)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.replace(tmp, dest / f"{account_id}.json")


def merge(old: Snapshot | None, fresh: dict[str, Window], now: float) -> Snapshot:
    """A payload may omit a window that is still running; keep the old one until it expires."""
    kept = {k: w for k, w in (old.windows if old else {}).items() if w.resets_at > now}
    return Snapshot(now, {**kept, **fresh})
