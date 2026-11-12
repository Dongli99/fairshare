"""Where state lives. Snapshots are plain JSON, one per account, written atomically."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from .model import SessionRecord, SessionSeen, Snapshot, Window, Worker


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


def merge(old: Snapshot | None, fresh: dict[str, Window], now: float,
          probe: bool = False) -> Snapshot:
    """A payload may omit a window that is still running; keep the old one until it expires.
    Idle sessions redraw with the last values they received, and usage never falls within one
    window, so a lower value for the same window is stale: keep the stored one. A fresh window
    already past its reset is a redraw from before the reset: never taken. The snapshot's time
    says when its numbers were last known to be current: it moves when a window's value or
    reset changed or a window appeared, or when they came from the provider's own usage probe
    (`probe`), never for a redraw of equal values, so idle redraws cannot make it look fresh."""
    kept = {k: w for k, w in (old.windows if old else {}).items() if w.resets_at > now}
    taken = {k: w for k, w in fresh.items()
             if w.resets_at > now                  # a redraw of a window that already reset
             and not (k in kept and w.used_pct < kept[k].used_pct
                     and abs(w.resets_at - kept[k].resets_at) <= SAME_WINDOW_S)}
    changed = any(kept.get(k) != w for k, w in taken.items())
    return Snapshot(now if probe or changed or old is None else old.captured_at,
                    {**kept, **taken})

def record(account_id: str, fresh: dict[str, Window], now: float,
           probe: bool = False) -> Snapshot:
    """Merge `fresh` into the stored snapshot and save it, holding the account's lock, so two
    captures arriving together cannot both read the old snapshot and the last one win. `probe`:
    the windows came from the provider's own usage probe (see `merge`)."""
    dest = state_dir() / "snapshots"
    dest.mkdir(parents=True, exist_ok=True)
    with open(dest / f"{account_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snap = merge(load(account_id), fresh, now, probe)
        save(account_id, snap)
    return snap


def claim_probe(account_id: str, now: float, max_age: float) -> bool:
    """Whether a background usage probe for the account may start now; if so, it is marked as
    started. One whose mark is under `max_age` seconds old is taken to be running still."""
    dest = state_dir() / "probes"
    dest.mkdir(parents=True, exist_ok=True)
    mark = dest / f"{account_id}.started"
    with locked(dest / ".lock"):
        try:
            if now - mark.stat().st_mtime < max_age:
                return False
        except FileNotFoundError:
            pass
        mark.touch()
        os.utime(mark, (now, now))
    return True


SESSION_KEEP_S = 7 * 24 * 3600   # a session record unseen this long is dropped
SESSION_WRITE_S = 60             # an unchanged record is rewritten at most this often
_SESSION_ID = re.compile(r"[\w][\w.-]*")   # a session id is also a file name here


def _sessions() -> Path:
    return state_dir() / "sessions"


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _read_session(path: Path) -> SessionRecord | None:
    try:
        d = json.loads(path.read_text())
        return SessionRecord(path.stem, str(d["account"]), d.get("cwd"), d.get("transcript"),
                             float(d["seen"]), None if d.get("stranded_at") is None
                             else float(d["stranded_at"]), _text(d.get("role")),
                             _text(d.get("parent")),
                             _num(d.get("checkpoint_requested")), _num(d.get("request_said")),
                             d["warned"] if isinstance(d.get("warned"), dict) else {},
                             d["reported"] if isinstance(d.get("reported"), dict) else {})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


_SESSION_BASE = ("account", "cwd", "transcript", "seen", "stranded_at")


def _write_session(dest: Path, rec: SessionRecord) -> None:
    """The record's file: the base fields always, the others only when set."""
    _write_json(dest, f"{rec.id}.json", {k: v for k, v in vars(rec).items()
                                         if k in _SESSION_BASE or (k != "id" and v)})


def session(session_id: str) -> SessionRecord | None:
    """The record of one session, or None."""
    if not _SESSION_ID.fullmatch(session_id):
        return None
    return _read_session(_sessions() / f"{session_id}.json")


def sessions() -> list[SessionRecord]:
    """Every session record that reads."""
    found = sorted(_sessions().glob("*.json")) if _sessions().is_dir() else []
    return [r for p in found if (r := _read_session(p))]


def _drop_old(dest: Path, now: float) -> None:
    for p in dest.glob("*.json"):
        if (r := _read_session(p)) and now - r.seen > SESSION_KEEP_S:
            with contextlib.suppress(OSError):
                p.unlink()


def note_session(seen: SessionSeen, account_id: str, now: float,
                 stranded: bool | None = None) -> SessionRecord | None:
    """Record that `seen` runs on `account_id`, keeping its folder and transcript (a field the
    provider left out keeps its old value) and every other field. `stranded` True marks it
    stopped by its account's limit now, False clears that, None leaves it. Written only when it
    is new or changed, or last seen SESSION_WRITE_S ago or more, so a redraw costs no write;
    each write drops the records unseen for SESSION_KEEP_S. Under a lock: a hook and a usage
    hook may arrive together. None for an id that cannot be a file name."""
    if not _SESSION_ID.fullmatch(seen.id):
        return None
    dest = _sessions()
    dest.mkdir(parents=True, exist_ok=True)
    with locked(dest / ".lock"):
        old = _read_session(dest / f"{seen.id}.json")
        new = replace(old or SessionRecord(seen.id, account_id, None, None, now),
                      account=account_id, cwd=seen.cwd or (old and old.cwd),
                      transcript=seen.transcript or (old and old.transcript), seen=now,
                      stranded_at=now if stranded else None if stranded is False
                      else old and old.stranded_at)
        if old and replace(old, seen=now) == new and now - old.seen < SESSION_WRITE_S:
            return old
        _write_session(dest, new)
        _drop_old(dest, now)
    return new


def update_session(session_id: str, change) -> SessionRecord | None:
    """Apply `change` (the record, or None when there is none -> the new record, or None for no
    change) under the sessions' lock, so a hook and `watch` cannot both act on the old record.
    The record as it stands afterwards (None when there is none)."""
    if not _SESSION_ID.fullmatch(session_id):
        return None
    dest = _sessions()
    dest.mkdir(parents=True, exist_ok=True)
    with locked(dest / ".lock"):
        old = _read_session(dest / f"{session_id}.json")
        new = change(old)
        if new is None or new == old:
            return old
        _write_session(dest, new)
    return new


def _workers() -> Path:
    return state_dir() / "workers"


def _read_worker(path: Path) -> Worker | None:
    try:
        d = json.loads(path.read_text())
        return Worker(path.stem, str(d["parent"]), str(d["account"]), str(d["cwd"]),
                      float(d["started"]), _text(d.get("session")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _save_worker(w: Worker) -> None:
    _write_json(_workers(), f"{w.name}.json", {k: v for k, v in vars(w).items() if k != "name"})


def add_worker(w: Worker) -> bool:
    """Record a new worker; False when a worker of that name is recorded already (its name
    must be unique: it is how its session is found). Drops the records started SESSION_KEEP_S
    ago or more."""
    if not _SESSION_ID.fullmatch(w.name):
        return False
    dest = _workers()
    dest.mkdir(parents=True, exist_ok=True)
    with locked(dest / ".lock"):
        for p in dest.glob("*.json"):
            if (old := _read_worker(p)) and w.started - old.started > SESSION_KEEP_S:
                with contextlib.suppress(OSError):
                    p.unlink()
        if (dest / f"{w.name}.json").exists():
            return False
        _save_worker(w)
    return True


def drop_worker(name: str) -> None:
    """Forget a worker that never started."""
    with contextlib.suppress(OSError), locked(_workers() / ".lock"):
        (_workers() / f"{name}.json").unlink()


def worker(name: str) -> Worker | None:
    if not _SESSION_ID.fullmatch(name):
        return None
    return _read_worker(_workers() / f"{name}.json")


def workers(parent: str | None = None) -> list[Worker]:
    """The recorded workers (of the main session `parent`, when given), oldest first."""
    found = sorted(_workers().glob("*.json")) if _workers().is_dir() else []
    return sorted((w for p in found if (w := _read_worker(p))
                   and (parent is None or w.parent == parent)), key=lambda w: (w.started, w.name))


def join_worker(name: str, session_id: str) -> None:
    """Note the session id a worker turned out to have."""
    with locked(_workers() / ".lock"):
        if (w := worker(name)) and w.session != session_id:
            _save_worker(replace(w, session=session_id))


def _checkpoints() -> Path:
    return state_dir() / "checkpoints"


def checkpoint_path(session_id: str) -> Path | None:
    """Where a session's checkpoint note is (it may not exist); None for a bad id."""
    return _checkpoints() / f"{session_id}.md" if _SESSION_ID.fullmatch(session_id) else None


def checkpointed() -> list[str]:
    """The ids of the sessions that have a checkpoint note."""
    return sorted(p.stem for p in _checkpoints().glob("*.md")) if _checkpoints().is_dir() else []


def append_checkpoint(session_id: str, text: str) -> Path:
    """Append `text` to the session's checkpoint note, under a lock; never rewrites it."""
    path = checkpoint_path(session_id)
    if path is None:
        raise ValueError(f"not a session id: {session_id!r}")
    with locked(_checkpoints() / ".lock"), open(path, "a", encoding="utf-8") as f:
        f.write(text)
    return path


def _requests() -> Path:
    return state_dir() / "requests"


def save_request(launcher: int, session_id: str, manual: bool, pid: int | None) -> None:
    """Ask the launcher `launcher` (a process id) to continue `session_id` elsewhere: one file
    per launcher, replaced atomically. `pid` is the session's own process, when known, so the
    launcher acts only for its own child; `manual` skips its check that the account is out."""
    _write_json(_requests(), f"{launcher}.json",
                {"session": session_id, "manual": manual, "pid": pid})


def take_request(launcher: int) -> tuple[str, bool, int | None] | None:
    """(session id, manual, pid) of the launcher's pending request, removed as it is read; None
    when there is none or it does not read."""
    path = _requests() / f"{launcher}.json"
    try:
        d = json.loads(path.read_text())
        path.unlink()
        pid = d.get("pid")
        return str(d["session"]), d.get("manual") is True, pid if isinstance(pid, int) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


@contextlib.contextmanager
def locked(path: Path):
    """Hold an exclusive lock on the file `path` (created if missing) while the block runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def held(lock: Path, seconds: float = 10.0, stale: float | None = None):
    """Hold the lock FOLDER `lock` (made with mkdir, as some tools do for their own files) while
    the block runs. Waits up to `seconds` for it; one untouched for more than `stale` seconds is
    left over and is taken over. Raises OSError when it stays held."""
    end = time.monotonic() + seconds
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                old = stale is not None and time.time() - lock.stat().st_mtime > stale
            except FileNotFoundError:
                continue                     # released meanwhile: try again
            if old:
                with contextlib.suppress(OSError):
                    os.rmdir(lock)           # an empty folder; a race just means waiting on
                continue
            if time.monotonic() > end:
                raise OSError(f"{lock} is held by someone else; try again later")
            time.sleep(0.1)
    try:
        yield
    finally:
        os.rmdir(lock)


def _trust_only_file() -> Path:
    return state_dir() / "trust-only.json"


def trust_only(folder: Path) -> bool:
    """Whether the user answered "only" to the trust question for `folder` (canonical)."""
    try:
        return json.loads(_trust_only_file().read_text()).get(str(folder)) is True
    except (OSError, ValueError, AttributeError):
        return False


def remember_trust_only(folder: Path) -> None:
    """Remember "only" for `folder`: one small JSON map, rewritten atomically under a lock."""
    path = _trust_only_file()
    with locked(path.with_name(path.name + ".lock")):
        try:
            d = json.loads(path.read_text())
            d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            d = {}
        _write_json(state_dir(), path.name, {**d, str(folder): True})


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
