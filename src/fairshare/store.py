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

from .model import Request, SessionRecord, SessionSeen, Snapshot, Window, Worker


def config_path() -> Path:
    return Path(os.environ.get("FAIRSHARE_CONFIG",
                               Path.home() / ".config/fairshare/accounts.toml"))


def state_dir() -> Path:
    return Path(os.environ.get("FAIRSHARE_STATE", Path.home() / ".local/state/fairshare"))


def load(account_id: str) -> Snapshot | None:
    """The account's snapshot; None when there is none or it cannot be read. A window whose
    numbers are not numbers is left out, as if never seen, so that the next probe refills it."""
    try:
        d = json.loads((state_dir() / "snapshots" / f"{account_id}.json").read_text())
        windows = {k: Window(pct, at) for k, v in d["windows"].items() if isinstance(v, dict)
                   and (pct := _num(v.get("used_pct"))) is not None
                   and (at := _num(v.get("resets_at"))) is not None}
        return Snapshot(float(d["captured_at"]), windows) if _num(d["captured_at"]) is not None \
            else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
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


def _full_resets_file() -> Path:
    return state_dir() / "full-reset.json"


def full_resets() -> dict[str, tuple[float, float | None]]:
    """The planned full resets, by account id: (the account's weekly reset when it was planned,
    when the reset was seen used or None). An entry that does not read is left out."""
    try:
        d = json.loads(_full_resets_file().read_text())
    except (OSError, ValueError):
        return {}
    return {k: (week, _num(v.get("used_at"))) for k, v in d.items()
            if isinstance(v, dict) and (week := _num(v.get("week_resets_at"))) is not None} \
        if isinstance(d, dict) else {}


def active_full_resets(now: float) -> dict[str, float]:
    """The accounts whose planned full reset is still to be used this week, with their weekly
    reset: not seen used and that week not over. A plan never carries into the next week."""
    return {k: week for k, (week, used) in full_resets().items() if used is None and now < week}


def _change_full_resets(change) -> None:
    """Apply `change` (the plans as stored -> the plans to store) under the file's lock and
    write the result atomically."""
    path = _full_resets_file()
    with locked(path.with_name(path.name + ".lock")):
        plans = change({k: {"week_resets_at": week, "used_at": used}
                        for k, (week, used) in full_resets().items()})
        _write_json(state_dir(), path.name, plans)


def plan_full_reset(account_id: str, week_resets_at: float) -> None:
    """Plan a full reset on the account this week, replacing any plan it had."""
    _change_full_resets(lambda d: {**d, account_id: {"week_resets_at": week_resets_at,
                                                     "used_at": None}})


def drop_full_reset(account_id: str | None = None) -> None:
    """Drop the account's planned full reset (every account's with None)."""
    _change_full_resets(lambda d: {k: v for k, v in d.items()
                                   if account_id is not None and k != account_id})


SAME_WINDOW_S = 3600   # resets this close together belong to the same window
PROBE_ROUNDING = 1.0   # the probe's text gives whole percents: a fall this small is rounding


def merge(old: Snapshot | None, fresh: dict[str, Window], now: float,
          probe: bool = False) -> Snapshot:
    """A payload may omit a window that is still running; keep the old one until it expires.
    Idle sessions redraw with the last values they received, so a lower value for the same
    window from a redraw is stale: keep the stored one. Usage can still fall within a window
    (the provider credits some back), and the provider's own usage probe (`probe`) is current
    by construction: its lower value for the same window is taken when it falls by more than
    rounding, under the stored reset (the probe gives the reset to the minute only). A fresh
    window already past its reset is a redraw from before the reset: never taken. The
    snapshot's time says when its numbers were last known to be current: it moves when a
    window's value or reset changed or a window appeared, or when they came from a probe, never
    for a redraw of equal values, so idle redraws cannot make it look fresh."""
    kept = {k: w for k, w in (old.windows if old else {}).items() if w.resets_at > now}
    taken = {}
    for k, w in fresh.items():
        if w.resets_at <= now:                     # a redraw of a window that already reset
            continue
        was = kept.get(k)
        if was and w.used_pct < was.used_pct and abs(w.resets_at - was.resets_at) <= SAME_WINDOW_S:
            if not probe or was.used_pct - w.used_pct <= PROBE_ROUNDING:
                continue                           # stale, or rounding
            w = Window(w.used_pct, was.resets_at)  # credited back
        taken[k] = w
    changed = any(kept.get(k) != w for k, w in taken.items())
    return Snapshot(now if probe or changed or old is None else old.captured_at,
                    {**kept, **taken})

def record(account_id: str, fresh: dict[str, Window], now: float,
           probe: bool = False) -> Snapshot:
    """Merge `fresh` into the stored snapshot and save it, holding the account's lock, so two
    captures arriving together cannot both read the old snapshot and the last one win. `probe`:
    the windows came from the provider's own usage probe (see `merge`). An active planned full
    reset of the account is marked used, in the same locked step, when a running window fell
    (only the probe's credited-back path lowers one) or its reset moved: within a window usage
    only rises and a full reset keeps the reset times. A small credit-back taken for the reset
    only ends the planned bonus early."""
    dest = state_dir() / "snapshots"
    dest.mkdir(parents=True, exist_ok=True)
    with open(dest / f"{account_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = load(account_id)
        snap = merge(old, fresh, now, probe)
        save(account_id, snap)
        if old and account_id in active_full_resets(now) and any(
                (w := snap.windows.get(k)) and (w.used_pct < was.used_pct
                                                or abs(w.resets_at - was.resets_at) > SAME_WINDOW_S)
                for k, was in old.windows.items() if was.resets_at > now):
            _change_full_resets(lambda d: {**d, account_id: {**d[account_id], "used_at": now}}
                                if account_id in d else d)
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


def _costs(v: object) -> list:
    """A cost series as stored; [] for anything that is not one."""
    if not isinstance(v, list) or not all(
            isinstance(s, list) and s and all(isinstance(p, list) and len(p) == 2
                                              and _num(p[0]) is not None
                                              and _num(p[1]) is not None for p in s) for s in v):
        return []
    return v


COST_POINT_S = 300     # a new cost point once the last one is this old (the latest value is kept)
COST_KEEP_S = 7200     # cost points older than this are dropped, one kept before as a baseline


def add_cost(series: list, t: float, cost: float) -> list:
    """`series` (oldest first: segments of [time, cost] points) with the running cost `cost` seen
    at `t`. A cost that moved adds a point when the last one is COST_POINT_S old or more, else
    becomes the last point's value (a segment's first point is never changed: it is the base);
    a cost lower than the last means the session started counting again: a new segment from it,
    never a negative spend. Points older than COST_KEEP_S go, except the one before the oldest
    kept (the base its rise is measured from) and the latest value."""
    segs = [[list(p) for p in seg] for seg in series]
    last = segs[-1][-1] if segs else None
    if last is None or cost < last[1]:
        segs.append([[t, cost]])
    elif cost > last[1]:
        if len(segs[-1]) == 1 or t - last[0] >= COST_POINT_S:
            segs[-1].append([t, cost])
        else:
            last[1] = cost
    cut, kept = t - COST_KEEP_S, []
    for i, seg in enumerate(segs):
        while len(seg) >= 2 and seg[1][0] <= cut:
            seg.pop(0)
        if seg[-1][0] > cut or i == len(segs) - 1:
            kept.append(seg)
    return kept


def spend(series: list, start: float, end: float) -> float | None:
    """The cost rise of a session inside (start, end]: each rise between two points counts at
    the later point's time (at most COST_POINT_S late). None when the session has no cost data."""
    if not series:
        return None
    return sum(q[1] - p[1] for seg in series for p, q in zip(seg, seg[1:]) if start < q[0] <= end)


def total_cost(series: list) -> float | None:
    """A session's cost as far as the series knows: the last value of each segment (each one
    counts from zero again)."""
    return sum(seg[-1][1] for seg in series) if series else None


def _drawn(v: object) -> dict:
    """Drawn windows as stored; {} for anything that is not them."""
    if not isinstance(v, dict) or not all(
            isinstance(ws, dict) and all(isinstance(w, list) and len(w) == 2
                                         and all(_num(x) is not None for x in w)
                                         for w in ws.values()) for ws in v.values()):
        return {}
    return v


def _limits(v: object) -> dict:
    """Limits as stored ({account: reset}); {} for anything that is not them."""
    if not isinstance(v, dict) or not all(_num(x) is not None for x in v.values()):
        return {}
    return {k: float(x) for k, x in v.items()}


def _stopped(v: object) -> list:
    """A turn end as stored ([time, background tasks]); [] for anything that is not one."""
    if not isinstance(v, list) or len(v) != 2 or _num(v[0]) is None or _num(v[1]) is None:
        return []
    return [float(v[0]), int(v[1])]


def _read_session(path: Path) -> SessionRecord | None:
    try:
        d = json.loads(path.read_text())
        return SessionRecord(path.stem, str(d["account"]), d.get("cwd"), d.get("transcript"),
                             float(d["seen"]), None if d.get("stranded_at") is None
                             else float(d["stranded_at"]), _text(d.get("role")),
                             _text(d.get("parent")),
                             _num(d.get("checkpoint_requested")), _num(d.get("request_said")),
                             d["warned"] if isinstance(d.get("warned"), dict) else {},
                             d["reported"] if isinstance(d.get("reported"), dict) else {},
                             _text(d.get("model")), _text(d.get("model_name")),
                             _text(d.get("effort")), _num(d.get("waiting_until")),
                             _costs(d.get("costs")), _text(d.get("continues")),
                             _text(d.get("moved_to")), _drawn(d.get("drawn")),
                             _limits(d.get("limits")), _stopped(d.get("stopped")),
                             _text(d.get("parent_said")) or _text(d.get("parent")))
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
    """Record that `seen` runs on `account_id`, keeping its transcript (a field the provider
    left out keeps its old value) and every other field. Its folder is the first one seen: the
    one it started in, where the provider finds it to resume (the folder a hook names moves with
    the session's shell). `stranded` True marks it stopped by its account's limit now, False
    clears that, None leaves it. Written only when it
    is new or changed, or last seen SESSION_WRITE_S ago or more, so a redraw costs no write;
    a change of `stranded` either way also ends a wait for the provider's own resume;
    each write drops the records unseen for SESSION_KEEP_S. Under a lock: a hook and a usage
    hook may arrive together. None for an id that cannot be a file name. A model `seen` names
    replaces the known model, display name and effort together (the user may change them
    mid-session); none named keeps them."""
    if not _SESSION_ID.fullmatch(seen.id):
        return None
    dest = _sessions()
    dest.mkdir(parents=True, exist_ok=True)
    with locked(dest / ".lock"):
        old = _read_session(dest / f"{seen.id}.json")
        new = replace(old or SessionRecord(seen.id, account_id, None, None, now),
                      account=account_id, cwd=(old and old.cwd) or seen.cwd,
                      transcript=seen.transcript or (old and old.transcript), seen=now,
                      stranded_at=now if stranded else None if stranded is False
                      else old and old.stranded_at)
        if seen.model:
            new = replace(new, model=seen.model, model_name=seen.model_name, effort=seen.effort)
        if seen.cost is not None:
            new = replace(new, costs=add_cost(new.costs, now, seen.cost))
        if stranded is not None:
            new = replace(new, waiting_until=None)
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


def drawn_news(seen: SessionSeen, account_id: str, fresh: dict[str, Window],
               now: float) -> dict[str, Window]:
    """The windows in a usage hook's `fresh` that are news: changed since this session last
    drew them on `account_id` (all of them on its first draw there). An idle session redraws
    the last values it received, so a window it repeats says nothing, however it compares with
    the snapshot (after a probe that lowered it, say). Each draw is kept in the session's
    record (created when there is none), written only when it changed."""
    def change(old: SessionRecord | None) -> SessionRecord:
        rec = old or SessionRecord(seen.id, account_id, seen.cwd, seen.transcript, now)
        drawn = {k: [w.used_pct, w.resets_at] for k, w in fresh.items()}
        before = rec.drawn.get(account_id, {})
        news.update({k: w for k, w in fresh.items() if before.get(k) != drawn[k]})
        return replace(rec, drawn={account_id: drawn})
    news: dict[str, Window] = {}
    if not _SESSION_ID.fullmatch(seen.id):
        return dict(fresh)
    update_session(seen.id, change)
    return news


def move_session(session_id: str, account_id: str) -> SessionRecord | None:
    """A session resumed under its own id on `account_id`: its record names that account, is
    no longer stranded or waiting; every other field (role, parent, checkpoints' requests) is
    kept."""
    return update_session(session_id, lambda old: old and replace(
        old, account=account_id, stranded_at=None, waiting_until=None))


CHAIN_STEPS = 8    # `moved_to` links followed at most, so a damaged chain never loops


def latest(session_id: str) -> str:
    """The end of the chain of recorded sessions that continue `session_id` under new ids
    (`moved_to`), itself when it has not moved; stops at a cycle or after CHAIN_STEPS links."""
    chain = [session_id]
    while len(chain) <= CHAIN_STEPS and (r := session(chain[-1])) and r.moved_to \
            and r.moved_to not in chain and session(r.moved_to):
        chain.append(r.moved_to)
    return chain[-1]


def link_session(old_id: str, seen: SessionSeen, account_id: str, now: float,
                 copy: bool = False) -> SessionRecord | None:
    """`seen` continues the session `old_id` under a new id on `account_id` (a resume on another
    account, or a copy). Its record takes the old one's role, parent, model, display name and
    effort where it lacks them (a model `seen` names wins, as in `note_session`), the limits
    that stopped it, the account, its first folder (as `note_session` keeps it) and `seen`'s
    transcript, `continues=old_id`, and keeps a cost series of its own; the old checkpoint note
    is copied when the new id has none. When
    the old session has stopped (not `copy`), the new one takes its place: the old record keeps its account and costs, gets
    `moved_to`, and is no longer stranded or waiting, and workers (their records and their
    sessions' records) whose parent is the old id now name the new one. A `copy` of a session
    that may still run leaves the old record and its workers as they are. None, with nothing
    changed, when either id is bad, they are the same, the old record is missing or has moved
    already, or the new one already continues a session."""
    if old_id == seen.id or not _SESSION_ID.fullmatch(old_id) \
            or not _SESSION_ID.fullmatch(seen.id):
        return None
    dest = _sessions()
    dest.mkdir(parents=True, exist_ok=True)
    with locked(dest / ".lock"):
        old = _read_session(dest / f"{old_id}.json")
        mine = _read_session(dest / f"{seen.id}.json")
        if old is None or old.moved_to or (mine and mine.continues):
            return None
        new = mine or SessionRecord(seen.id, account_id, None, None, now)
        model = (seen.model, seen.model_name, seen.effort) if seen.model else \
            (new.model, new.model_name, new.effort) if new.model else \
            (old.model, old.model_name, seen.effort or old.effort)
        new = replace(new, account=account_id, cwd=new.cwd or seen.cwd or old.cwd,
                      transcript=seen.transcript or new.transcript, seen=now,
                      role=new.role or old.role, parent=new.parent or old.parent,
                      model=model[0], model_name=model[1], effort=model[2], continues=old_id,
                      limits={**old.limits, **new.limits})
        _write_session(dest, new)
        if not copy:
            _write_session(dest, replace(old, moved_to=seen.id, stranded_at=None,
                                         waiting_until=None))
            for p in dest.glob("*.json"):
                if (r := _read_session(p)) and r.parent == old_id and r.id != seen.id:
                    _write_session(dest, replace(r, parent=seen.id))
            with locked(_workers() / ".lock"):
                for w in workers(old_id):
                    _save_worker(replace(w, parent=seen.id))
    src, dst = checkpoint_path(old_id), checkpoint_path(seen.id)
    with locked(_checkpoints() / ".lock"):
        if src.is_file() and not dst.exists():
            fd, tmp = tempfile.mkstemp(dir=_checkpoints())
            os.close(fd)
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
    return new


def _workers() -> Path:
    return state_dir() / "workers"


def _read_worker(path: Path) -> Worker | None:
    try:
        d = json.loads(path.read_text())
        return Worker(path.stem, str(d["parent"]), str(d["account"]), str(d["cwd"]),
                      float(d["started"]), _text(d.get("session")), _text(d.get("model")),
                      _text(d.get("effort")), _num(d.get("cost")), _num(d.get("duration")))
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


def finish_worker(name: str, cost: float | None, duration: float) -> bool:
    """Record a worker's measured cost and duration (when it ended or went still); True when
    that changed its record."""
    with locked(_workers() / ".lock"):
        if (w := worker(name)) is None or (w.cost, w.duration) == (cost, duration):
            return False
        _save_worker(replace(w, cost=cost, duration=duration))
    return True


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


def save_request(launcher: int, req: Request) -> None:
    """Ask the launcher `launcher` (a process id) to act on its child's session: one file per
    launcher, replaced atomically. The request's `pid` is the session's own process, when known,
    so the launcher acts only for its own child; `manual` skips its check that the account is
    out."""
    _write_json(_requests(), f"{launcher}.json", vars(req))


def take_request(launcher: int) -> Request | None:
    """The launcher's pending request, removed as it is read; None when there is none or it
    does not read."""
    path = _requests() / f"{launcher}.json"
    try:
        d = json.loads(path.read_text())
        path.unlink()
        pid = d.get("pid")
        return Request(str(d["session"]), d.get("manual") is True,
                       pid if isinstance(pid, int) else None, _text(d.get("kind")) or "pickup",
                       _text(d.get("model")), _text(d.get("effort")), _text(d.get("account")),
                       _text(d.get("name")))
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
