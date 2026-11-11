"""Where state lives. Snapshots are plain JSON, one per account, written atomically."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import tempfile
import time
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


def _write_json(dest: Path, name: str, data: dict) -> None:
    """Write `dest/name` atomically."""
    dest.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.replace(tmp, dest / name)


def save(account_id: str, snap: Snapshot) -> None:
    _write_json(state_dir() / "snapshots", f"{account_id}.json",
                {"captured_at": snap.captured_at,
                 "windows": {k: vars(v) for k, v in snap.windows.items()}})


def record_subscription(account_id: str, subscription: bool, now: float) -> None:
    """Remember the provider's last answer to "is this a subscription login?": a yes/no and the
    time, nothing else. Failing to write only means the account is never counted as confirmed."""
    try:
        _write_json(state_dir() / "checks", f"{account_id}.json",
                    {"subscription": subscription, "checked_at": now})
    except OSError:
        pass


def subscription_seen(account_id: str) -> tuple[bool, float] | None:
    """(answer, time) of the last successful sign-in check, or None when there never was one."""
    try:
        d = json.loads((state_dir() / "checks" / f"{account_id}.json").read_text())
        if isinstance(d["subscription"], bool):
            return d["subscription"], float(d["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_preference(account_id: str, until: float) -> None:
    _write_json(state_dir(), "prefer.json", {"account": account_id, "until": until})


def clear_preference() -> None:
    with contextlib.suppress(FileNotFoundError):
        (state_dir() / "prefer.json").unlink()


def preference(now: float) -> tuple[str, float] | None:
    """(account id, until) of the user's timed preference, None when there is none or it ended."""
    try:
        d = json.loads((state_dir() / "prefer.json").read_text())
        if isinstance(d["account"], str) and float(d["until"]) > now:
            return d["account"], float(d["until"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


SAME_WINDOW_S = 3600   # resets this close together belong to the same window


def merge(old: Snapshot | None, fresh: dict[str, Window], now: float) -> Snapshot:
    """A payload may omit a window that is still running; keep the old one until it expires.
    Idle sessions redraw with the last values they received, and usage never falls within one
    window, so a lower value for the same window is stale: keep the stored one. The snapshot's
    time moves only when something was taken, so stale redraws cannot make it look fresh. A
    fresh window already past its reset is a redraw from before the reset: never taken."""
    kept = {k: w for k, w in (old.windows if old else {}).items() if w.resets_at > now}
    taken = {k: w for k, w in fresh.items()
             if w.resets_at > now                  # a redraw of a window that already reset
             and not (k in kept and w.used_pct < kept[k].used_pct
                     and abs(w.resets_at - kept[k].resets_at) <= SAME_WINDOW_S)}
    return Snapshot(now if taken or old is None else old.captured_at, {**kept, **taken})



def record(account_id: str, fresh: dict[str, Window], now: float) -> Snapshot:
    """Merge `fresh` into the stored snapshot and save it, holding the account's lock, so two
    captures arriving together cannot both read the old snapshot and the last one win."""
    dest = state_dir() / "snapshots"
    dest.mkdir(parents=True, exist_ok=True)
    with open(dest / f"{account_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snap = merge(load(account_id), fresh, now)
        save(account_id, snap)
    return snap

@contextlib.contextmanager
def locked(path: Path):
    """Hold an exclusive lock on the file `path` (created if missing) while the block runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def free_name(path: Path) -> Path:
    """`path`, or the same name with -1, -2... added when that already exists."""
    found, n = path, 1
    while os.path.lexists(found):
        found, n = path.with_name(f"{path.name}-{n}"), n + 1
    return found


def write_with_backup(path: Path, text: str, backups: Path | None = None) -> Path | None:
    """Replace a file atomically, first copying any existing one beside it with a timestamp (or
    into the folder `backups`), then read it back. A symlink is written through to its target,
    never replaced by a regular file. Returns the backup's path, or None when there was nothing
    to back up."""
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        if backups is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = free_name(path.with_name(f"{path.name}.bak-fairshare-{stamp}"))
        else:
            backups.mkdir(parents=True, exist_ok=True)
            backup = free_name(backups / path.name)
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if backup:
        shutil.copymode(backup, tmp)
    os.replace(tmp, path)
    if path.read_text(encoding="utf-8") != text:
        raise OSError(f"{path}: what was read back differs from what was written")
    return backup
