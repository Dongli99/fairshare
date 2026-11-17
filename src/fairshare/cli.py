"""fairshare CLI: status | capture | run (the default) | prefer | pickup | handoff | transcript |
trust | worker | checkpoint | watch | setup | join | leave | uninstall | doctor."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import select
import shutil
import signal
import subprocess
import sys

if sys.platform == "win32":   # termios, tty and POSIX job control below: macOS and Linux only
    sys.exit("fairshare runs on macOS and Linux; Windows is not supported")

import termios
import time
import tomllib
import tty
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import browser
from . import hub as hub_mod
from . import providers, session, store
from .model import (ACCOUNT_ID, DEFAULT_HUB, EFFORT_NONE, NOTE_THINKING, NOTE_WRITING,
                    ROLE_WORKER, USAGE_MODEL_TURN, Account,
                    AccountStatus, ConfigError, Request, SessionRecord, SessionSeen, Snapshot,
                    Tuning, Window, Worker,
                    load_accounts, load_plan, window_model, with_account, with_hub,
                    without_account)
from .policy import HOUR, Verdict, prefer, rank


def _load():
    path = store.config_path()
    if not path.exists():
        raise SystemExit(f"fairshare: no config at {path} (run `fairshare setup`)")
    try:
        return load_accounts(path)
    except ConfigError as e:
        raise SystemExit(f"fairshare: {path}: {e}")


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _probe(accounts: list[Account]) -> list[AccountStatus | None]:
    """Ask every account's provider who is signed in, all at once."""
    if not accounts:
        return []
    with ThreadPoolExecutor(len(accounts)) as pool:
        return list(pool.map(lambda a: providers.get(a.provider).account_status(a), accounts))


def _check(accounts: list[Account], now: float
           ) -> list[tuple[AccountStatus | None, dict[str, Window] | str | None]]:
    """Ask every account's provider who is signed in and what it has used, all at once: the wait
    is the slowest answer, not their sum. Per account: its status, and its usage (windows, a
    reason it could not be read, or None when the account turned the probe off)."""
    if not accounts:
        return []
    with ThreadPoolExecutor(2 * len(accounts)) as pool:
        asked = [(pool.submit(providers.get(a.provider).account_status, a),
                  pool.submit(providers.get(a.provider).read_usage, a, now)) for a in accounts]
        return [(st.result(), use.result()) for st, use in asked]


def _record_usage(acct: Account, use: dict[str, Window] | str | None, now: float) -> str | None:
    """Record a probe's windows in the account's snapshot (same lock and merge as capture). Why
    its numbers did not come from the probe, or None when they did (or the probe is off)."""
    if not isinstance(use, dict):
        return use
    try:
        store.record(acct.id, use, now, probe=True)
    except OSError as e:
        return f"not recorded: {e.strerror or type(e).__name__}"
    return None


@dataclass
class Survey:
    verdicts: list[Verdict]                  # ranked, one per quota
    out: list[tuple[Account, str]]           # left out of the ranking, and why
    twins: list[tuple[Account, str]]         # folders sharing a login, and the id ranked for them
    notes: dict[str, list[str]]              # remarks per account id
    failed: list[str]                        # ids whose sign-in check could not be answered
    snaps: dict[str, Snapshot]
    tuning: Tuning
    now: float
    preference: tuple[str, float] | None = None   # the user's (account id, until), if any
    usage: dict[str, str | None] = field(default_factory=dict)   # probed ids: None = answered,
                                                                 # else why it failed
    expires: dict[str, float] = field(default_factory=dict)      # signed-in ids: sign-in's end


def _with_detected_plan(a: Account, plan_name: str) -> Account:
    """`a` on the plan the provider reported, keeping the account's own capacity override."""
    return replace(a, plan=load_plan(plan_name, a.capacity))


def _unconfirmed(a: Account) -> str | None:
    """Why an account whose sign-in check failed cannot be used, or None when it can: the last
    successful check must have said "subscription". A login never confirmed as one might be
    billed per use. (A login file is no evidence: a sign-in may store none.)"""
    seen = store.subscription_seen(a.id)
    if not (seen and seen[0]):
        return f"sign-in check failed and {a.id} was never confirmed as a subscription login"
    return None


NOT_SIGNED_IN = "not signed in"
NO_ACCOUNTS = "no accounts yet: `fairshare join <name>` signs one in"
FALLBACK = "went by its last confirmation as a subscription login and the plan in accounts.toml"


def _no_hub(hub: Path) -> str:
    return f"no hub at {hub}; if you moved it, run `fairshare doctor --fix`"


def _require_hub(hub: Path) -> None:
    """Stop before anything runs against a hub that is not there (a sign-in check would create
    folders at the old path)."""
    if not hub_mod.present(hub):
        raise SystemExit(f"fairshare: {_no_hub(hub)}")


def _survey() -> Survey:
    """Check every account once, then rank those that can take work."""
    accounts, tuning, hub = _load()
    if not accounts:                         # before the hub: none to start, nothing to create
        raise SystemExit(f"fairshare: {NO_ACCOUNTS}")
    _require_hub(hub)
    now = _now()
    live, twins, notes, failed, usage, expires = [], [], {}, [], {}, {}
    out = [(a, a.plan_error) for a in accounts if a.plan is None]     # no plan file: not ranked
    planned = [a for a in accounts if a.plan is not None]
    for a, (st, use) in zip(planned, _check(planned, now)):
        if use is not None:
            usage[a.id] = _record_usage(a, use, now)
        if st is None:
            failed.append(a.id)
            if why := _unconfirmed(a):
                out.append((a, why))
            else:
                when = time.strftime("%Y-%m-%d", time.localtime(store.subscription_seen(a.id)[1]))
                notes[a.id] = [f"sign-in check failed; last confirmed as a subscription login "
                               f"on {when}; plan {a.plan.name} from config"]
                live.append((a, None))
            continue
        store.record_subscription(a.id, st.logged_in and st.usable, now)
        if not st.logged_in:
            out.append((a, NOT_SIGNED_IN))
        elif not st.usable:
            out.append((a, "not a subscription login"))
        else:
            if st.plan and st.plan != a.plan.name:
                notes[a.id] = [f"plan {a.plan.name} in config, detected {st.plan}"]
                a = _with_detected_plan(a, st.plan)
            if st.expires_at is not None:
                expires[a.id] = st.expires_at
            live.append((a, st))
    snaps = {a.id: s for a in accounts if (s := store.load(a.id))}    # with this run's probes
    # One login in several folders is one quota: rank it once, by its freshest snapshot.
    ranked, seen = [], {}
    for a, st in sorted(live, key=lambda x: x[0].id):
        if st is None or st.identity is None:
            ranked.append(a)
            continue
        key = (a.provider, st.identity)
        same = [m for m, ms in live if ms and (m.provider, ms.identity) == key]
        rep = max(sorted(same, key=lambda m: m.id),
                  key=lambda m: snaps[m.id].captured_at if m.id in snaps else -1.0)
        if seen.setdefault(key, rep).id == a.id:
            ranked.append(a)
        else:
            twins.append((a, seen[key].id))
    verdicts = rank(ranked, snaps, now, tuning)
    if pref := store.preference(now):
        verdicts = prefer(verdicts, pref[0])
    return Survey(verdicts, out, twins, notes, failed, snaps, tuning, now, pref, usage, expires)


def _in(h: float | None) -> str:
    return "-" if h is None else f"{int(h)}h{int(h % 1 * 60):02d}m"


def _clock(t: float, now: float) -> str:
    """A local clock time, with the day name when it is not today. Unlike "in 2h", it stays true
    on a line that is not redrawn."""
    lt = time.localtime(t)
    return time.strftime("%H:%M" if lt[:3] == time.localtime(now)[:3] else "%a %H:%M", lt)


def _day_clock(t: float) -> str:
    """A local clock time, always with the day name."""
    return time.strftime("%a %H:%M", time.localtime(t))


LOGIN_WARN_DAYS = 3        # a sign-in ending this soon is named at every run and status


def _expiry_lines(expires: dict[str, float], now: float) -> dict[str, str]:
    """For each account whose sign-in ends within LOGIN_WARN_DAYS: one line saying when, and the
    fix."""
    return {i: f"{i}: sign-in ends {_day_clock(t)}; `fairshare join {i}` signs it in again"
            for i, t in expires.items() if t - now < LOGIN_WARN_DAYS * 24 * HOUR}


def _probe_line(acct_id: str, reason: str, snap: Snapshot | None, now: float) -> str:
    """What a failed usage probe means for the ranking. A probe that ran as a model turn spent
    usage, so it says so loudly, every time, with the way to stop it."""
    where = (f"using its snapshot from {_clock(snap.captured_at, now)}" if snap
             else "no snapshot; ranked as a fresh window")
    if reason == USAGE_MODEL_TURN:
        return (f"warning: the usage probe for {acct_id} ran as a model turn and may spend usage "
                f"at every launch; its answer is not used, {where}; set `usage_args = []` in "
                f"{acct_id}'s accounts.toml table to stop it")
    return f"usage probe failed for {acct_id} ({reason}); {where}"


def _probe_lines(s: Survey) -> dict[str, str]:
    """A line for each ranked account whose usage probe failed."""
    return {v.account.id: _probe_line(v.account.id, why, s.snaps.get(v.account.id), s.now)
            for v in s.verdicts if (why := s.usage.get(v.account.id))}


def _age(s: Survey, acct_id: str) -> str:
    """For a row whose numbers did not come from this run's probe: how old they are."""
    snap = s.snaps.get(acct_id)
    if not snap or (acct_id in s.usage and s.usage[acct_id] is None):
        return ""
    return f"  (usage as of {_clock(snap.captured_at, s.now)})"


def _spent_models(acct_id: str, snap: Snapshot | None, t: Tuning, now: float) -> list[str]:
    """A warning for each per-model week of the account that is spent and not yet reset. It
    changes nothing in the ranking: model choice is the user's."""
    return [f"warning: {acct_id}'s {model} week is spent until {_clock(w.resets_at, now)}; "
            "other models are unaffected"
            for k, w in sorted((snap.windows if snap else {}).items())
            if (model := window_model(k)) and w.used_pct >= t.long_exhausted_pct
            and w.resets_at > now]


def _usable(v: Verdict, now: float) -> str:
    """When a spent account can take work again."""
    return f"usable at {_clock(now + v.usable_in * HOUR, now)}"


def _honoured(s: Survey) -> bool:
    return bool(s.preference and s.verdicts and s.verdicts[0].account.id == s.preference[0])


def _preference_note(s: Survey) -> str | None:
    if not s.preference:
        return None
    who, until = s.preference
    note = f"preferring {who} until {_clock(until, s.now)}"
    if not _honoured(s):
        note += "; it cannot take work now, so the best account is used"
    return note


LIVE_S = 600           # a session seen this recently is live (status's sessions block)
SPEND_SPAN_S = 3600    # spend is shown and compared over this span: the last hour
SESSIONS_SHOWN = 5     # status shows at most this many sessions per account


def _hour_spend(recs: list[SessionRecord], now: float) -> dict[str, float | None]:
    """Each session's spend in the last hour (None: no cost data), by id."""
    return {r.id: store.spend(r.costs, now - SPEND_SPAN_S, now) for r in recs}


def _role(rec: SessionRecord) -> str:
    """main, or a worker's name."""
    if rec.role != ROLE_WORKER:
        return "main"
    return w.name if (w := _worker_of(rec.id)) else ROLE_WORKER


def _about(rec: SessionRecord) -> tuple[str, str]:
    """A session's model (display name) and folder (its last part), `-` when unknown."""
    return rec.model_name or rec.model or "-", (Path(rec.cwd).name if rec.cwd else "") or "-"


def _sessions_block(acct_id: str, recs: list[SessionRecord], now: float) -> list[str]:
    """status's lines for an account's live sessions, heaviest in the last hour first, at most
    SESSIONS_SHOWN, then `+N more`: id start, model, folder, role, spend and its share of the
    account's last-hour spend; `-` with no cost data. A session that went on under a new id
    (`moved_to`) is not listed; its spend still counts for its own account."""
    mine = [r for r in recs if r.account == acct_id]
    spent = _hour_spend(mine, now)
    total = sum(x for x in spent.values() if x)
    live = sorted((r for r in mine if now - r.seen <= LIVE_S and not r.moved_to),
                  key=lambda r: (-(spent[r.id] if spent[r.id] is not None else -1), r.id))
    lines = []
    for r in live[:SESSIONS_SHOWN]:
        x = spent[r.id]
        model, folder = _about(r)
        lines.append(f"session {r.id[:8]} · {model} · {folder} · {_role(r)} · " + (
            "-" if x is None else f"${x:.2f}" + (f" ({100 * x / total:.0f}%)" if total else "")))
    if len(live) > SESSIONS_SHOWN:
        lines.append(f"+{len(live) - SESSIONS_SHOWN} more")
    return lines


def _heaviest(rec: SessionRecord, acct_id: str, now: float) -> str:
    """One sentence naming another session of the account that drew at least half of its
    last-hour spend; "" when none did."""
    mine = [r for r in store.sessions() if r.account == acct_id]
    spent = _hour_spend(mine, now)
    total = sum(x for x in spent.values() if x)
    heavy = max(((spent[r.id], r.id, r) for r in mine if r.id != rec.id and spent[r.id]
                 and 2 * spent[r.id] >= total), default=None, key=lambda h: h[:2])
    if not heavy:
        return ""
    model, folder = _about(heavy[2])
    return (f" Heaviest in the last hour: session {heavy[1][:8]} ({model}, {folder}, "
            f"{100 * heavy[0] / total:.0f}%).")


def cmd_status(_a) -> int:
    s = _survey()
    if s.preference and not _honoured(s):
        print(_preference_note(s))
    probes, expiring = _probe_lines(s), _expiry_lines(s.expires, s.now)
    recs = store.sessions()
    for i, v in enumerate(s.verdicts):
        mark, aid = (">" if i == 0 else " "), v.account.id
        flag = "" if v.eligible else f"  [avoid] {_usable(v, s.now)}"
        if i == 0 and _honoured(s):
            flag = f"  [preferred until {_clock(s.preference[1], s.now)}]"
        print(f"{mark} {aid:<12} {v.account.plan.name:<14} score {v.score:6.2f}  "
              f"{v.reason}{_age(s, aid)}{flag}")
        for note in [*s.notes.get(aid, []), *([probes[aid]] if aid in probes else []),
                     *_spent_models(aid, s.snaps.get(aid), s.tuning, s.now),
                     *([expiring[aid]] if aid in expiring else []),
                     *_sessions_block(aid, recs, s.now)]:
            print(f"    {note}")
    for a, why in s.out:
        print(f"  {a.id:<12} {a.plan.name if a.plan else '-':<14} left out: {why}")
        for note in [*s.notes.get(a.id, []), *_sessions_block(a.id, recs, s.now)]:
            print(f"    {note}")
    for a, rep in s.twins:
        print(f"  {a.id:<12} {a.plan.name:<14} same login as {rep}")
        for note in [*([expiring[a.id]] if a.id in expiring else []),
                     *_sessions_block(a.id, recs, s.now)]:
            print(f"    {note}")
    return 0


def _nothing_to_rank(s: Survey) -> None:
    if not s.verdicts:
        why = "; ".join(f"{a.id}: {w}" for a, w in s.out)
        raise SystemExit(f"fairshare: no account can take work ({why})")


def _session_account(accounts: list[Account]) -> Account | None:
    """The account whose hub folder this process's session runs in, else None (a session started
    some other way, outside the hub)."""
    for a in accounts:
        prov = providers.REGISTRY.get(a.provider)
        d = prov and prov.session_dir()
        if d and os.path.realpath(d) == os.path.realpath(a.config_dir):
            return a
    return None


TIGHT, SPENT, PLAIN = "\033[33m", "\033[31m", "\033[0m"   # yellow, red, reset


def _span(hours: float) -> str:
    return f"{hours / 24:g}d" if hours % 24 == 0 else f"{hours:g}h"


def _stale(snap: Snapshot | None, t: Tuning, now: float) -> bool:
    """Whether an account's usage is old enough to ask for again (or unknown)."""
    return snap is None or now - snap.captured_at > t.refresh_minutes * 60


def _status_line(acct: Account, snap: Snapshot | None, now: float, live: bool,
                 t: Tuning) -> str:
    """The account and its windows, shortest first, named by their length (`5h 23%`). A window
    at the hot mark turns yellow, red once spent, and shows when it resets: a tight account
    about to reset is still worth using. A window the plan has but whose usage is unknown (none
    seen yet, or reset since: nothing is known until the next exchange) shows `-`, so the line
    keeps its shape. A per-model week shows only once hot, named by its model (`Fable 96%`). A
    session's first redraws carry no usage yet, so the saved snapshot stands in, marked with
    when it was taken; so are numbers older than `refresh_minutes` (an idle session redraws
    them unchanged)."""
    p = acct.plan
    hours = {p.short_name: p.short_hours, p.long_name: p.long_hours} if p else {}
    known = {k: w for k, w in (snap.windows if snap else {}).items() if w.resets_at > now}
    names = [k for k in hours if k] + [k for k in known if k not in hours and (
        not window_model(k) or known[k].used_pct >= t.short_hot_pct)]
    parts = [acct.id]
    for k in sorted(names, key=lambda k: (bool(window_model(k)), hours.get(k) or 0)):
        label = _span(hours[k]) if hours.get(k) else window_model(k) or k
        w = known.get(k)
        if w is None:
            parts.append(f"{label} -")
            continue
        text = f"{label} {w.used_pct:.0f}%"
        if w.used_pct >= t.short_hot_pct:
            colour = SPENT if w.used_pct >= t.long_exhausted_pct else TIGHT
            text = f"{colour}{text} ({_clock(w.resets_at, now)}){PLAIN}"
        parts.append(text)
    if known and (not live or _stale(snap, t, now)):
        parts.append(f"as of {_clock(snap.captured_at, now)}")
    return " · ".join(parts)


def cmd_capture(a) -> int:
    """Runs as the provider's usage hook. Must never raise or print noise. The account is the
    session's own folder in the hub; a session outside the hub records nothing. With --then, the
    user's own command for that hook gets the same stdin and prints the line. An account id after
    `capture` (the form before the hub) is ignored. When the session was stopped by its
    account's limit and that account still cannot take work, the line ends in the pickup hint
    (on a line of its own after the user's)."""
    raw = sys.stdin.buffer.read()
    out = None
    try:
        accounts, tuning, _ = _load()
        acct = _session_account(accounts)
        prov = acct and providers.get(acct.provider)
        payload = json.loads(raw)
        fresh = acct and prov.parse_capture(payload)
        now = _now()
        seen = acct and prov.session_of(payload)
        if fresh and seen:
            fresh = store.drawn_news(seen, acct.id, fresh, now)
        snap = store.record(acct.id, fresh, now) if fresh else acct and store.load(acct.id)
        if seen:
            with contextlib.suppress(OSError):
                out = _stranded(acct, seen, snap, accounts, tuning, now)
        if acct and _stale(snap, tuning, now):
            _start_probe(acct, seen and seen.cwd, now)
        if acct and not a.then:
            print(_status_line(acct, snap, now, bool(fresh), tuning) + (f" · {out}" if out else ""))
    except (Exception, SystemExit):      # whatever is wrong, the user's own line must still run
        pass
    if a.then:
        sys.stdout.flush()
        code = subprocess.run(a.then, shell=True, input=raw).returncode
        if out:
            print(out)
        return code
    return 0


PICKUP_MARK = f"{SPENT}out · ! fairshare pickup{PLAIN}"


def _pickup_mark(rec: SessionRecord, acct: Account, snap: Snapshot | None,
                 accounts: list[Account], t: Tuning, now: float) -> str:
    """The bar's mark for a stranded session: when it waits for the provider's own resume,
    which limit, when that is and where a pickup would move it now."""
    if rec.waiting_until is None or rec.waiting_until <= now:
        return PICKUP_MARK
    lim = _limit(acct, snap, t, now)
    best = _best_usable(acct, accounts, t, now)[0]
    return (f"{SPENT}out · {f'{lim[0]} limit · ' if lim else ''}resumes "
            f"{_clock(rec.waiting_until, now)} · "
            + (f"! fairshare pickup moves it to {best.account.id} now" if best
               else "no account can take work now") + PLAIN)


def _limit(acct: Account, snap: Snapshot | None, t: Tuning, now: float
           ) -> tuple[str, float] | None:
    """Which limit holds the account and when it lifts: of its plan windows at their limit (a
    week at `long_exhausted_pct`, a short window at `short_hot_pct`) still running, the one that
    resets last, as (its span for people, `5h`, or its name; its reset); None when no window
    says."""
    p = acct.plan
    return max(((_span(hours) if hours else name, w.resets_at)
                for name, hours, full in ((p.long_name, p.long_hours, t.long_exhausted_pct),
                                          (p.short_name, p.short_hours, t.short_hot_pct))
                if name and snap and (w := snap.windows.get(name)) and w.used_pct >= full
                and w.resets_at > now), key=lambda x: x[1], default=None) if p else None


def _stranded(acct: Account, seen, snap: Snapshot | None, accounts: list[Account], t: Tuning,
              now: float) -> str | None:
    """Record the session; the pickup hint when its limit stopped it and its account still
    cannot take work by the policy. Numbers newer than the stop that say it can work again (a
    window reset) clear the mark, and any wait for the provider's own resume with it."""
    _continuation(seen, acct, now)
    rec = store.note_session(seen, acct.id, now)
    if rec is None or rec.stranded_at is None or acct.plan is None:
        return None
    if not rank([acct], {acct.id: snap} if snap else {}, now, t)[0].eligible:
        return _pickup_mark(rec, acct, snap, accounts, t, now)
    if snap and snap.captured_at > rec.stranded_at:
        store.note_session(seen, acct.id, now, stranded=False)
    return None


def _fairshare_argv() -> list[str]:
    """This very fairshare, as a command a child process can run (not through PATH; `-P`: not
    from the folder it runs in)."""
    return [sys.executable, "-P", "-m", "fairshare.cli"]


def _start_probe(acct: Account, cwd: str | None, now: float) -> bool:
    """Start one detached `fairshare probe <id>` and never wait for it: its own session, stdin
    and output on /dev/null, in the session's folder (as the launch probe runs in the launch's).
    At most one per account at a time (`store.claim_probe`). True when one was started."""
    try:
        if not store.claim_probe(acct.id, now, 2 * providers.get(acct.provider).usage_timeout):
            return False
        subprocess.Popen([*_fairshare_argv(), "probe", acct.id],
                         cwd=cwd if cwd and os.path.isdir(cwd) else None,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def _quietly(work) -> int:
    """Run `work()` printing nothing and raising nothing; exit 0. For what the provider runs on
    its own (hooks) and what runs detached (the background probe)."""
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), \
            contextlib.redirect_stderr(quiet):
        try:
            work()
        except (Exception, SystemExit):
            pass
    return 0


def cmd_probe(a) -> int:
    """Hidden, started by capture: ask the provider for one account's usage now and record it
    as a probe. A probe that fails records nothing, so the bar ages honestly."""
    def work():
        accounts, _, hub = _load()
        acct = next(x for x in accounts if x.id == a.id)
        if not hub_mod.present(hub, acct.id):        # it would create folders at the old path
            return
        now = _now()
        if (use := providers.get(acct.provider).read_usage(acct, now)) and isinstance(use, dict):
            store.record(acct.id, use, now, probe=True)
    return _quietly(work)


def cmd_hook(a) -> int:
    """Runs as one of fairshare's own hooks in the provider's settings (`hook <name>`), with the
    hook's input on stdin. Never raises and exits 0. It prints only what a handler gives (the
    provider's own answer that puts a line in the model's context), else nothing."""
    said: list[str | None] = []

    def work():
        raw = json.loads(sys.stdin.buffer.read())
        if handle := HOOKS.get(a.name):
            said.append(handle(a.name, raw))
    _quietly(work)
    if said and said[0]:
        print(said[0])
    return 0


LAUNCHER_ENV = "FAIRSHARE_LAUNCHER"   # set by the launcher for its child: its own process id


def _ancestors() -> set[int]:
    """The process ids above this one, up to (not including) init. Empty when `ps` cannot say."""
    try:
        r = subprocess.run([shutil.which("ps", path="/bin:/usr/bin") or "ps", "-A", "-o",
                            "pid=,ppid="], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return set()
    parent = {int(a): int(b) for a, b in (line.split() for line in r.stdout.splitlines()
                                          if len(line.split()) == 2) if a.isdigit() and b.isdigit()}
    found, p = set(), os.getppid()
    while p > 1 and p not in found:
        found.add(p)
        p = parent.get(p, 0)
    return found


def _launcher() -> int | None:
    """The fairshare launcher this process runs under: LAUNCHER_ENV, when it names one of this
    process's ancestors (so a stale or inherited value never signals a stranger)."""
    try:
        pid = int(os.environ.get(LAUNCHER_ENV, ""))
    except ValueError:
        return None
    return pid if pid > 1 and pid in _ancestors() else None


CONTINUES_ENV = "FAIRSHARE_CONTINUES"   # set by the launcher for a resume: "<its pid>:<old id>"
COPY = ":copy"     # after the old id in CONTINUES_ENV: the resume is a copy (`_may_run`)


def _continuation(seen: SessionSeen, acct: Account, now: float) -> None:
    """Called before a session is recorded from inside it: when the launcher resumed a session
    and it goes on under a new id (another account: decision 23, or a copy: `_may_run`), link the
    two (`store.link_session`; a copy as a copy). Only when CONTINUES_ENV names one of this
    process's ancestors (a value leaked into a nested session never links a stranger), its old
    id is not `seen`'s, and `seen` has no record or one not yet linked. Never raises."""
    try:
        pid, _, old = os.environ.get(CONTINUES_ENV, "").partition(":")
        old, copy = old.removesuffix(COPY), old.endswith(COPY)
        if not old or old == seen.id or ((rec := store.session(seen.id)) and rec.continues):
            return
        if int(pid) > 1 and int(pid) in _ancestors():
            store.link_session(old, seen, acct.id, now, copy)
    except (Exception, SystemExit):
        pass


# What a request asks the launcher to do after it stopped its child: resume the same session on
# the best account; start a fresh one that reads the checkpoint (written, or to be written by
# the launcher first); start a fresh one with nothing (a switch).
PICKUP, HANDOFF, HANDOFF_NOTE, SWITCH = "pickup", "handoff", "handoff-note", "switch"
STOP = "stop"   # the launcher's own: the same limit stopped the session again; end, saying how
                # to resume it later (`_pickup_request`)


def _request(launcher: int, req: Request) -> None:
    """Ask the launcher to stop its child and act on its session."""
    store.save_request(launcher, req)
    os.kill(launcher, signal.SIGUSR1)


def _stop_failure(name: str, raw: object) -> None:
    """`hook stop-failure`: the account's limit ended a turn. Mark the session stranded, ask for
    the account's usage in the background (the bar's mark and the launcher's check need it), and
    when a launcher started this session, ask it for a pickup. Says nothing."""
    accounts, _, _ = _load()
    if (acct := _session_account(accounts)) is None:
        return
    prov = providers.get(acct.provider)
    if (seen := prov.hook_session(name, raw)) is None:
        return
    now = _now()
    _continuation(seen, acct, now)
    store.note_session(seen, acct.id, now, stranded=True)
    _start_probe(acct, seen.cwd, now)
    if launcher := _launcher():
        _request(launcher, Request(seen.id, False, prov.session_pid()))


# --- What the hooks say (decision 22): percent crossings, once each per window per session,
# and a main session's request for a checkpoint, passed on to its worker.

WARN_LEVELS = (90, 95)   # a plan window crossing one of these is said once per window and session


def _crossing(rec: SessionRecord, acct: Account, snap: Snapshot, now: float
              ) -> tuple[tuple[str, Window, int] | None, dict]:
    """The highest crossing of WARN_LEVELS in the account's plan windows not yet warned for this
    session in that window (by its reset), as (window, its numbers, level); and the session's
    warned levels with every new crossing marked (a higher one covers the lower)."""
    p, best, warned = acct.plan, None, dict(rec.warned)
    for k in (p.short_name, p.long_name):
        w = k and snap.windows.get(k)
        if not w or w.resets_at <= now:
            continue
        level = max((x for x in WARN_LEVELS if w.used_pct >= x), default=0)
        old = rec.warned.get(k)
        same = (isinstance(old, list) and len(old) == 2
                and abs(float(old[0]) - w.resets_at) <= store.SAME_WINDOW_S)
        if not level or (same and float(old[1]) >= level):
            continue
        warned[k] = [w.resets_at, level]
        if best is None or (level, w.used_pct) > (best[2], best[1].used_pct):
            best = (k, w, level)
    return best, warned


def _tell_crossing(session_id: str, acct: Account, t: Tuning, now: float
                   ) -> tuple[str, Window, int] | None:
    """A crossing to say for the session, marked as said on its record in the same locked step
    (a hook and `watch` both ask: whichever comes first says it). Numbers older than twice
    `refresh_minutes` say nothing new (stale numbers must not move anyone) and start the
    background probe, as capture does."""
    snap = store.load(acct.id)
    if snap is None or now - snap.captured_at > 2 * t.refresh_minutes * 60:
        _start_probe(acct, None, now)
        return None
    got = []

    def change(old: SessionRecord | None) -> SessionRecord | None:
        best, warned = _crossing(old, acct, snap, now) if old else (None, {})
        if best is None:
            return None
        got.append(best)
        return replace(old, warned=warned)
    store.update_session(session_id, change)
    return got[0] if got else None


def _short_state(acct: Account, snap: Snapshot | None, now: float) -> str:
    """An account's plan windows in a few words (`5h 10% · 7d 40%`), unknown ones as `-`."""
    p = acct.plan
    parts = []
    for k, hours in ((p.short_name, p.short_hours), (p.long_name, p.long_hours)):
        w = k and snap and snap.windows.get(k)
        if k:
            parts.append(f"{_span(hours) if hours else k} "
                         + (f"{w.used_pct:.0f}%" if w and w.resets_at > now else "-"))
    return " · ".join(parts)


def _best_usable(acct: Account, accounts: list[Account], t: Tuning, now: float
                 ) -> tuple[Verdict | None, dict[str, Snapshot]]:
    """The best other account that can take work, by the local snapshots alone (no sign-in
    check, no probe), or None; and those snapshots."""
    others = [a for a in accounts if a.plan is not None and a.id != acct.id]
    snaps = {a.id: s for a in others if (s := store.load(a.id))}
    return next((v for v in rank(others, snaps, now, t) if v.eligible), None), snaps


def _best_other(acct: Account, accounts: list[Account], t: Tuning, now: float) -> str:
    """The best other account by the local snapshots alone, with its windows in a few words."""
    v, snaps = _best_usable(acct, accounts, t, now)
    return (f"{v.account.id} ({_short_state(v.account, snaps.get(v.account.id), now)})" if v
            else "none can take work now")


def _warning_text(rec: SessionRecord, acct: Account, crossing: tuple[str, Window, int],
                  accounts: list[Account], t: Tuning, now: float) -> str:
    """What a crossing says, by the session's role. Provider-neutral."""
    k, w, _ = crossing
    p = acct.plan
    hours = p.short_hours if k == p.short_name else p.long_hours
    window = _span(hours) if hours else k
    if rec.role == ROLE_WORKER:
        return (f"fairshare: this account is at {w.used_pct:.0f}% of its {window} window. You are "
                'a worker: finish your current step, then run fairshare checkpoint "<what is '
                'done, what is next>". Do not start a big new step; your main session decides '
                "what happens next.")
    workers = ", ".join(x.name for x in store.workers(rec.id)) or "none"
    return (f"fairshare: account {acct.id} is at {w.used_pct:.0f}% of its {window} window (resets "
            f"{_clock(w.resets_at, now)}).{_heaviest(rec, acct.id, now)} Best other account: "
            f"{_best_other(acct, accounts, t, now)}"
            ". If what is left of your work is small, carry on. Otherwise, at the next stopping "
            'point: write a checkpoint (fairshare checkpoint "<where you stand>") and run '
            "fairshare pickup to continue on the best account. Workers you started: "
            f"{workers}; to have them checkpoint: fairshare checkpoint --request all.")


CHECKPOINT_ASK = ("fairshare: your main session asks for a checkpoint now: finish your current "
                  'step and run fairshare checkpoint "<what is done, what is next>".')


def _ask_checkpoint(session_id: str) -> bool:
    """Whether a worker's pending checkpoint request is to be passed on now: once per request,
    marked in the same locked step."""
    got = []

    def change(old: SessionRecord | None) -> SessionRecord | None:
        if (old is None or old.role != ROLE_WORKER or old.checkpoint_requested is None
                or old.request_said == old.checkpoint_requested):
            return None
        got.append(True)
        return replace(old, request_said=old.checkpoint_requested)
    store.update_session(session_id, change)
    return bool(got)


def _speak(name: str, raw: object) -> str | None:
    """`hook post-tool` and `hook prompt`: what to put in the session's context, as the
    provider's hook answer, or None. Silent inside a subagent. A worker with a pending request
    hears it; otherwise a new crossing of its account is said for the session's role. Reads only
    local files, except that the prompt hook looks pending workers up by name."""
    accounts, tuning, _ = _load()
    if (acct := _session_account(accounts)) is None or acct.plan is None:
        return None
    prov = providers.get(acct.provider)
    if (seen := prov.hook_session(name, raw)) is None:
        return None
    now = _now()
    _continuation(seen, acct, now)
    rec = store.session(seen.id)
    if name == PROMPT_HOOK and (rec is None or rec.role is None):
        if _join_pending(store.workers(), accounts, now):
            rec = store.session(seen.id)
    rec = rec or store.note_session(seen, acct.id, now)
    if rec is None:
        return None
    if name == PROMPT_HOOK and rec.role != ROLE_WORKER:
        _measure_still(store.workers(rec.id), now)
    if _ask_checkpoint(rec.id):
        return prov.hook_output(name, CHECKPOINT_ASK)
    if crossing := _tell_crossing(rec.id, acct, tuning, now):
        return prov.hook_output(name, _warning_text(rec, acct, crossing, accounts, tuning, now))
    return None


STOP_FAILURE, POST_TOOL_HOOK, PROMPT_HOOK = "stop-failure", "post-tool", "prompt"
HOOKS = {STOP_FAILURE: _stop_failure,   # fairshare's hook name -> handler(name, input): what to
         POST_TOOL_HOOK: _speak,        # print, or None
         PROMPT_HOOK: _speak}


def _say(text: str) -> None:
    print(f"fairshare: {text}", file=sys.stderr)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _grace_warning(v: Verdict, snaps: dict[str, Snapshot], tuning: Tuning, now: float) -> bool:
    """An account that is eligible only because its hot short window resets soon. True when
    the warning was said."""
    w = (snap := snaps.get(v.account.id)) and snap.windows.get(v.account.plan.short_name)
    if w and w.used_pct >= tuning.short_hot_pct and 0 < w.resets_at - now:
        left = (w.resets_at - now) / HOUR
        if left < tuning.grace_hours:
            _say(f"warning: {v.account.id} {v.account.plan.short_name} window "
                 f"{w.used_pct:.0f}% used, resets in {int(left * 60)}m")
            return True
    return False


def _choose(prompt: str, valid: set[str]) -> str | None:
    """One of `valid`, asked until given: there is no default. None at end of input."""
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            print(file=sys.stderr)
            return None
        if answer in valid:
            return answer


def _offer_trust(start: Account, explicit: bool) -> bool:
    """On a terminal, before the start check: offer to trust this folder for the accounts that
    lack it, as the provider's own dialog would. False when the user says no (exit 1, nothing
    written). Never in HOME (the provider keeps trust there per session), never without a
    terminal (the provider then asks for itself). `run -a` asks about its account only; a plain
    launch asks about every account, unless the starting one is trusted and an earlier "only"
    answer for this folder stands."""
    if not _interactive():
        return True
    here = Path(os.getcwd())
    accounts, _, hub = _load()
    pool = [start] if explicit else [a for a in accounts if a.plan is not None]
    lacking = [a for a in pool if providers.get(a.provider).trusts(a.config_dir, here) is not True]
    if not lacking:
        return True
    tool = providers.get(start.provider).tool
    if here == Path.home().resolve():
        _say(f"In HOME, fairshare does not ask; {tool}'s own per-session question applies")
        return True
    starts = start.id in {a.id for a in lacking}
    if not explicit and not starts and store.trust_only(here):
        return True
    _say(f"{tool} will be able to read, edit and execute files in {here}.")
    if explicit:
        options = {"y": [start]}
        print(f"  [y] trust it for {start.id}\n  [n] no, exit", file=sys.stderr)
    else:
        options = {"a": lacking, "o": [start] if starts else []}
        print(f"  [a] trust it for all accounts lacking it ({', '.join(a.id for a in lacking)})\n"
              + (f"  [o] only {start.id}\n  [n] no, exit" if starts
                 else "  [o] only the accounts that already trust it"), file=sys.stderr)
    keys = [*options, *(["n"] if starts else [])]
    answer = _choose(f"Trust {here}? [{'/'.join(keys)}] ", set(keys))
    if answer in (None, "n"):
        if not starts:
            return True                  # nothing to trust for the account starting: it starts
        _say(f"{start.id} not started; nothing written")
        return False
    if answer == "o" and not explicit:
        store.remember_trust_only(here)
    for a in options[answer]:
        try:
            key = providers.get(a.provider).trust(a.config_dir, here,
                                                  hub_mod.backup_dir(hub, f"trust-{a.id}"))
            _say(f"{a.id} now trusts {key}")
        except (OSError, UnicodeDecodeError, ValueError) as e:
            _say(f"{a.id}: trust not written ({e}); {tool} will ask for itself")
    return True


def _args_for(args, acct: Account) -> list[str]:
    """A session's arguments: a list, or built once its account is known (a function of it)."""
    return args(acct) if callable(args) else args


def _launch(v: Verdict, args, explicit: bool = False, continues: str | None = None) -> int:
    """Check the hub is there, offer folder trust, run the start check on the account's hub
    folder, then start the provider as a child, in its clean environment (with the account's
    browser and LAUNCHER_ENV naming this process), and return its exit code. `args`: a list, or
    a function of the account that builds them now. `continues`: the id of the session this
    start resumes, passed as CONTINUES_ENV (with this process's id) so a new id links to it;
    any inherited value is dropped otherwise; a resume on an account whose limit stopped that
    session is refused until the limit lifts (`_held`), whatever chose it. A request (SIGUSR1,
    `_pickup_request`) stops the child and acts on its session (`_continue`), with this process
    still the parent, so a later limit picks up again."""
    prov, acct = providers.get(v.account.provider), v.account
    hub = acct.config_dir.parent.parent
    rec = continues and store.session(continues.removesuffix(COPY))
    if until := _held(rec, acct.id, now := _now()):
        _say(f"{acct.id}'s limit stopped this session and lifts at {_clock(until, now)}; not "
             f"resuming it there before then. To resume it later: {_resume_line(rec)}")
        return 75
    if not hub_mod.present(hub, acct.id):
        _say(_no_hub(hub))
        return 1
    print(f"fairshare -> {acct.id} ({v.reason})", file=sys.stderr)
    if not _offer_trust(acct, explicit):
        return 1
    try:
        for note in hub_mod.check(hub, acct.id, prov):
            _say(note)
    except OSError as e:
        _say(f"cannot prepare {acct.id}'s folder in the hub ({e}); not started")
        return 1
    cmd, env = prov.launch(acct)
    env = {**{k: x for k, x in env.items() if k != CONTINUES_ENV}, **_browser_env(hub, acct),
           LAUNCHER_ENV: str(os.getpid())}
    if continues:
        env[CONTINUES_ENV] = f"{os.getpid()}:{continues}"
    if dropped := prov.dropped_login_vars():
        _say(f"ignoring {', '.join(dropped)} from the environment; the account's own login is used")
    if passed := prov.passed_session_vars():
        _say(f"passing {', '.join(passed)} through from the surrounding session")
    asked: list[tuple[SessionRecord, Survey | None, Request]] = []

    def requested(child: int) -> bool:
        if got := _pickup_request(child):
            asked.append(got)
        return bool(got)
    code = session.run_child(cmd + _args_for(args, acct), env, requested)
    if code != session.PICKUP:
        return code
    return _continue(*asked[-1])


COUNTDOWN_S = 10       # seconds the launcher shows before an automatic pickup starts


def _verdict(s: Survey, acct_id: str) -> Verdict | None:
    """The account's verdict in the survey (the twin ranked for its login's), or None."""
    rep = next((r for a, r in s.twins if a.id == acct_id), acct_id)
    return next((v for v in s.verdicts if v.account.id == rep), None)


def _can_work(s: Survey, acct_id: str) -> bool:
    """Whether the account (or the twin ranked for its login) can take work in the survey."""
    v = _verdict(s, acct_id)
    return v is not None and v.eligible


def _survey_limit(s: Survey, acct_id: str) -> tuple[str, float] | None:
    """`_limit` for the account in the survey."""
    v = _verdict(s, acct_id)
    return v and _limit(v.account, s.snaps.get(v.account.id), s.tuning, s.now)


def _held(rec: SessionRecord | None, acct_id: str, now: float) -> float | None:
    """When the limit that stopped `rec` on the account lifts, while it has not yet: no resume
    there before then. None otherwise."""
    until = rec.limits.get(acct_id) if rec else None
    return until if until is not None and until > now else None


def _hold(session_id: str, acct_id: str, until: float, now: float) -> None:
    """Write the limit that stopped the session on its record (`limits`); lifted ones go."""
    store.update_session(session_id, lambda old: old and replace(old, limits={
        **{k: x for k, x in old.limits.items() if x > now}, acct_id: until}))


def _pickup_request(child: int) -> tuple[SessionRecord, Survey | None, Request] | None:
    """Called in the launcher's wait on SIGUSR1. The request for this launcher, when it is for
    its own child's session and stands: a manual one (`fairshare pickup`, `handoff`, a switch)
    as it is; an automatic one only when the session is marked stranded and a fresh survey
    (probes included) confirms its account cannot take work. That limit is then written on the
    record (`_hold`). By default the session is left for the provider's own resume at the reset
    (`_waits`): the wait is written as `waiting_until`, said once per limit (`_wait_text`), and
    the child runs on. A move (opted in with `resume_wait_minutes`) when the same limit stopped
    the session before and has not lifted: a STOP request, so the launcher ends with the resume
    line instead of trying again. Otherwise None: back to waiting, nothing printed."""
    try:
        got = store.take_request(os.getpid())
        if got is None or (got.pid is not None and got.pid != child):
            return None
        rec = store.session(got.session)
        if rec is None or got.manual:
            return (rec, None, got) if rec else None
        if rec.stranded_at is None:
            return None
        s = _survey()
        if _can_work(s, rec.account):
            return None
        lim, before = _survey_limit(s, rec.account), _held(rec, rec.account, s.now)
        if lim:
            _hold(rec.id, rec.account, lim[1], s.now)
        if (until := _waits(s, rec, got, lim)) is not None:
            store.update_session(rec.id, lambda old: old and replace(old, waiting_until=until))
            if before is None or abs(before - until) > store.SAME_WINDOW_S:
                _say(_wait_text(s, rec.account, lim))
            return None
        return rec, s, replace(got, kind=STOP) if before is not None else got
    except (Exception, SystemExit):
        return None


def _waits(s: Survey, rec: SessionRecord, req: Request, lim: tuple[str, float] | None
           ) -> float | None:
    """The reset to leave a stopped session waiting for, or None to move it. Waiting is the
    default whenever the provider resumes such a session by itself and the reset is known: it
    keeps its whole context and costs no re-read. `resume_wait_minutes` N opts in to a move when
    the reset is more than N minutes away (0: always), and only to an account that can take work
    with the session's model; with none, it waits."""
    v = _verdict(s, rec.account)
    if v is None or lim is None or not providers.get(v.account.provider).resumes_at_reset:
        return None
    wait = s.tuning.resume_wait_minutes
    if wait is None or lim[1] - s.now <= wait * 60 or not _having_model(s, _carry(rec, req)[0]):
        return lim[1]
    return None


def _wait_text(s: Survey, acct_id: str, lim: tuple[str, float]) -> str:
    """The launcher's one line when it leaves a stopped session for the provider's own resume:
    which limit, when, and where a pickup would move it now (or the soonest account)."""
    v = _verdict(s, acct_id)
    tool = providers.get(v.account.provider).tool
    best = next((x for x in s.verdicts if x.eligible), None)
    soon = min((x for x in s.verdicts if x is not v), key=lambda x: (x.usable_in, x.account.id),
               default=None)
    where = (f"! fairshare pickup moves it to {best.account.id} now" if best
             else "no account can take work now"
             + (f" (the soonest: {soon.account.id}, {_usable(soon, s.now)})" if soon else ""))
    return (f"{acct_id} is out ({lim[0]} limit) · {tool} resumes this session at "
            f"{_clock(lim[1], s.now)} · {where}")


def _limit_text(s: Survey, acct_id: str) -> str:
    """Which window stopped the account and when it resets, for the countdown line."""
    v = _verdict(s, acct_id)
    if v is None:
        return next((why for a, why in s.out if a.id == acct_id), "no usage known")
    if lim := _survey_limit(s, acct_id):
        return f"{lim[0]} resets {_clock(lim[1], s.now)}"
    return _usable(v, s.now)


def _read_key(text, seconds: int, keys: bytes) -> bytes | None:
    """On the terminal: `text(seconds left)` on one line, counting down, until Enter (b"\\n"),
    one of `keys` (that key, lower case) or the time is up (None). Keys are read one by one
    (cbreak mode, what was typed before is dropped) and the terminal is always restored."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        for left in range(seconds, 0, -1):
            print(f"\r{text(left)}", end="", file=sys.stderr, flush=True)
            end = time.monotonic() + 1
            while (wait := end - time.monotonic()) > 0:
                if select.select([fd], [], [], wait)[0]:
                    key = os.read(fd, 1)
                    if key in (b"\r", b"\n"):
                        return b"\n"
                    if key.lower() in [bytes([k]) for k in keys]:
                        return key.lower()
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print(file=sys.stderr)


def _countdown(text, seconds: int = COUNTDOWN_S) -> bool:
    """`_read_key` for a pickup: False when the user types `c`; True on Enter or when the time
    is up."""
    return _read_key(text, seconds, b"c") != b"c"


TIGHT_WEEK_PCT = 90       # a chosen account whose week is this full makes a launch pause
CLOSE_SCORE = 0.10        # so does a second-best account within this fraction of its score


def _worth_pausing(s: Survey, top: Verdict, warned: bool) -> bool:
    """Whether the choice is worth a pause: the chosen account is tight (week at
    TIGHT_WEEK_PCT, a hot short window, or a spent model week), the second-best eligible one is
    close behind, or a `warning:` line was said for this launch."""
    p, snap = top.account.plan, s.snaps.get(top.account.id)
    wins = {k: w for k, w in (snap.windows if snap else {}).items() if w.resets_at > s.now}
    tight = (wins.get(p.long_name) and wins[p.long_name].used_pct >= TIGHT_WEEK_PCT) or (
        p.short_name in wins and wins[p.short_name].used_pct >= s.tuning.short_hot_pct)
    spent = _spent_models(top.account.id, snap, s.tuning, s.now)
    second = next((v for v in s.verdicts if v.eligible and v is not top), None)
    close = second is not None and top.score > 0 and second.score >= top.score * (1 - CLOSE_SCORE)
    return bool(tight or spent or close or warned)


def _pause(s: Survey, top: Verdict) -> Verdict | None:
    """Show the eligible accounts, then count down `launch_pause` seconds: Enter or the time
    running out starts `top`, a digit starts that account, q quits (None)."""
    eligible = [v for v in s.verdicts if v.eligible][:9]
    for i, v in enumerate(eligible, 1):
        print(f"  {i} {'>' if v is top else ' '} {v.account.id:<12} {v.reason}{_age(s, v.account.id)}",
              file=sys.stderr)
    digits = "".join(str(i) for i in range(1, len(eligible) + 1))
    which = digits if len(digits) == 1 else f"{digits[0]}-{digits[-1]}"
    try:
        key = _read_key(lambda left: f"starting {top.account.id} in {left} s · Enter = now · "
                        f"{which} = that account · q = quit", max(1, round(s.tuning.launch_pause)),
                        digits.encode() + b"q")
    except (OSError, termios.error):         # not a terminal after all: no pause
        return top
    if key == b"q":
        return None
    return eligible[int(key) - 1] if key and key.isdigit() else top


def _pickup_prompt(rec: SessionRecord, now: float, handed: bool = False) -> str:
    """The first prompt of the session that continues `rec`. Provider-neutral. `first` is the
    one place a step that comes before the rest goes. `handed`: `rec` handed over with nothing
    to stop it (no launcher), so it may still run."""
    if handed:
        why = f"which handed over at {_clock(now, now)} and may still be running"
    elif rec.stranded_at is not None:
        why = (f"which stopped at {_clock(rec.stranded_at, now)} because account {rec.account} "
               "hit its usage limit")
    else:
        why = f"which was stopped at {_clock(now, now)} on account {rec.account} to move it"
    note = store.checkpoint_path(rec.id)
    first = [f"Read its checkpoint note {note} first; it is the session's own account of where "
             "it stood."] if note and note.is_file() else []
    return " ".join([
        f"This session continues session {rec.id}, {why}. Its folder: {rec.cwd}.", *first,
        f"Run `fairshare transcript {rec.id} --tail 40` to see where it "
        f"{'left off' if handed else 'stopped'}; read the full "
        f"transcript at {rec.transcript} only if something is unclear. Then continue the work. "
        "First say in one line what you are picking up."])


MOVED_PROMPT = ("fairshare moved this session from account {old} to account {new} because "
                "{why}. Continue where you left off.")
RESTARTED_PROMPT = ("fairshare restarted this session on account {new} because {why}; it is still "
                    "the best account. Continue where you left off.")


def _moved_prompt(rec: SessionRecord, new: str) -> str:
    """The first prompt of a session resumed on another account, or on the same one when that
    is still the best. Provider-neutral."""
    why = (f"{rec.account} hit its usage limit" if rec.stranded_at is not None
           else "a move was asked for")
    text = RESTARTED_PROMPT if new == rec.account else MOVED_PROMPT
    return text.format(old=rec.account, new=new, why=why)


def _resumed(rec: SessionRecord, copy: bool = False):
    """The arguments that resume `rec` on the chosen account, with the moved prompt (`_start`
    passes `continues` with them). Building them, just before the start, clears its stranded
    mark and wait; the account stays: the first capture writes it under the same id, and a new
    id (another account) is linked to this one (`_continuation`). `copy`: a copy under a new id
    instead (the provider's fork argument), and the record is left as it is."""
    def build(acct: Account) -> list[str]:
        prompt = _moved_prompt(rec, acct.id)
        if not copy:
            store.update_session(rec.id, lambda old: old and replace(
                old, stranded_at=None, waiting_until=None))
        return [*providers.get(acct.provider).resume_args(rec.id, copy), prompt]
    return build


def _may_run(rec: SessionRecord) -> bool:
    """Whether a pickup of `rec` continues a copy under a new id rather than the session in
    place: when it was not stopped by its limit it may still run, fairshare stops no session it
    did not start, and two processes on one session would interleave its transcript. Said in
    one line when true."""
    if rec.stranded_at is not None:
        return False
    _say(f"{rec.id[:8]} was not stopped by its limit and may still be running; continuing a copy "
         "under a new id (the original is left as it is)")
    return True


def _worker_of(session_id: str) -> Worker | None:
    return next((w for w in store.workers() if w.session == session_id), None)


def _carry(rec: SessionRecord | None, req: Request) -> tuple[str | None, str | None]:
    """The model and effort a session started in place of `rec` keeps: the latest a usage hook
    recorded, else (a worker no usage hook saw) what its main session chose for it, else none
    (the account's default). The request's flags override each."""
    model = effort = None
    if rec and rec.model:
        model, effort = rec.model, rec.effort
    elif rec and (w := _worker_of(rec.id)):
        model, effort = w.model, w.effort
    return req.model or model, req.effort or effort


def _with_model(args, model: str | None, effort: str | None):
    """`args` after the provider's arguments for `model` and `effort` (a function of the
    account, as `_launch` takes it); `args` itself when there are none."""
    if not model and not effort:
        return args
    return lambda acct: [*providers.get(acct.provider).model_args(model, effort),
                         *_args_for(args, acct)]


def _lacks_model(acct: Account, snap: Snapshot | None, model: str, t: Tuning,
                 now: float) -> bool:
    """Whether the account's own week for `model` is spent and not yet reset (stage 4's model
    weeks, as `_spent_models` reads them)."""
    prov = providers.get(acct.provider)
    return any((wm := window_model(k)) and prov.uses_week(model, wm)
               and w.used_pct >= t.long_exhausted_pct and w.resets_at > now
               for k, w in (snap.windows if snap else {}).items())


def _having_model(s: Survey, model: str | None) -> list[Verdict]:
    """The eligible verdicts, less those whose account has `model`'s week spent."""
    return [v for v in s.verdicts if v.eligible and not (
        model and _lacks_model(v.account, s.snaps.get(v.account.id), model, s.tuning, s.now))]


def _no_model_line(model: str) -> str:
    return f"no account that can take work has {model} left this week"


def _start(rec: SessionRecord, req: Request, args, moving: str | None = None,
           pause: bool = False, continues: str | None = None) -> int:
    """Start a session in place of `rec`, in the current folder, with `args` (a list or a
    function of the account), keeping its model and effort (`_carry`): on the request's account
    (`-a`: no ranking, the trust check kept) or on the best one. `continues` as `_launch`
    takes it."""
    model, effort = _carry(rec, req)
    if req.account:
        return _run_account(req.account, args, model, effort, continues)
    return _route(args, moving=moving, pause=pause, model=model, effort=effort,
                  continues=continues)


def _to_folder(rec: SessionRecord) -> None:
    if rec.cwd and os.path.isdir(rec.cwd):
        os.chdir(rec.cwd)


def _resume_line(rec: SessionRecord) -> str:
    """How to resume `rec` later, on its own account (the countdown's cancel, a switch)."""
    prov = next((providers.get(a.provider) for a in _load()[0] if a.id == rec.account),
                _provider())
    return f"fairshare run -a {rec.account} -- {shlex.join(prov.resume_args(rec.id))}"


def _continue(rec: SessionRecord, s: Survey | None, req: Request) -> int:
    """After the launcher stopped its child, in the session's folder, by the request's kind:
    - a switch: one line to resume the old session, then a fresh session (the launch pause
      applies);
    - a handoff: a fresh session with the pickup prompt, which reads the checkpoint first; for
      HANDOFF_NOTE the launcher has the stopped session write that note first, and anything
      wrong there is a pickup instead (one line why);
    - a pickup: for an automatic one on a terminal, a countdown (cancel says how to resume
      later); then the same session resumed on the best account (`_resumed`).
    - a STOP: one line saying the same limit stopped it again, and how to resume it later.
    First one line says what is happening (`_notice`), so the terminal is never blank."""
    if req.kind == STOP:
        _say(f"{rec.account} stopped this session again before its limit lifted "
             f"({_limit_text(s, rec.account)}); not trying again. To resume it later: "
             f"{_resume_line(rec)}")
        return 75
    _notice(rec, req)
    if req.kind == SWITCH:
        _say(f"to resume the old session: {_resume_line(rec)}")
        _to_folder(rec)
        return _start(rec, req, [], pause=True)
    if req.kind == HANDOFF or (req.kind == HANDOFF_NOTE and _write_note(rec, req)):
        _to_folder(rec)
        return _start(rec, req, [_pickup_prompt(rec, _now())])
    if s is not None and _interactive():
        best = next((v.account.id for v in s.verdicts if v.eligible), None)
        where = f"on {best}" if best else "(no account can take work now)"
        if not _countdown(lambda left: f"{rec.account} hit its limit ({_limit_text(s, rec.account)}"
                          f"). Continuing {where} in {left} s · Enter = now · c = cancel"):
            _say(f"not continued; to resume it later: {_resume_line(rec)}")
            return 0
    _to_folder(rec)
    return _start(rec, req, _resumed(rec), moving=None if s or req.kind != PICKUP
                  else rec.account, continues=rec.id)


VERBS = {PICKUP: "picking up", HANDOFF: "handing off", HANDOFF_NOTE: "handing off",
         SWITCH: "switching from"}


def _notice(rec: SessionRecord, req: Request) -> None:
    """The launcher's first line after it stopped its child for `req`: the verb, the session's
    first 8 characters, and the model's display name and effort it keeps, when known. Ends in
    `…`, except for a note to write, whose phases follow on their own line."""
    model, effort = _carry(rec, req)
    name = rec.model_name if model and model == rec.model and rec.model_name else model
    what = " · ".join(x for x in (name, effort if effort != EFFORT_NONE else None) if x)
    print(f"fairshare · {VERBS.get(req.kind, req.kind)} {rec.id[:8]}"
          f"{f' ({what})' if what else ''}{'' if req.kind == HANDOFF_NOTE else '…'}",
          file=sys.stderr, flush=True)


def _browser_env(hub: Path, acct: Account) -> dict[str, str]:
    """BROWSER for the account's sign-in and sessions: its launcher, rewritten when missing."""
    try:
        return browser.env(hub, acct.id, acct.browser)
    except OSError as e:
        _say(f"cannot write {acct.id}'s browser launcher ({e}); links open in the default browser")
        return {}


def _wait_until(t: float) -> None:
    while (left := t - _now()) > 0:
        _sleep(min(left, 60.0))      # short naps: a sleeping laptop must not oversleep the target


def _run_account(account_id: str, args, model: str | None = None,
                 effort: str | None = None, continues: str | None = None) -> int:
    """The user's explicit choice: no ranking, no refusal, no model filter. Only an account that
    is not signed in with a subscription is stopped; a signed-out one is offered a sign-in on a
    terminal. `args` as `_launch` takes them, after the arguments for `model` and `effort`;
    `continues` as `_launch` takes it."""
    accounts, tuning, hub = _load()
    acct = next((x for x in accounts if x.id == account_id), None)
    if acct is None:
        _say(f"no account '{account_id}' (configured: {', '.join(x.id for x in accounts)})")
        return 2
    if acct.plan is None:
        _say(f"{acct.id}: {acct.plan_error}")
        return 1
    _require_hub(hub)
    prov = providers.get(acct.provider)
    ((st, use),) = _check([acct], _now())
    if st is None:
        if why := _unconfirmed(acct):
            _say(f"{acct.id}: {why}")
            return 1
        _say(f"sign-in check failed for {acct.id}; {FALLBACK}")
    else:
        store.record_subscription(acct.id, st.logged_in and st.usable, _now())
        if not st.logged_in:
            asked = _interactive() and _ask_yes(f"{acct.id} is signed out. Sign in now?")
            if not asked:
                _say(f"{acct.id} is not signed in; `fairshare join {acct.id}` signs it in again")
                return 1
            if (st := _resign(acct, accounts, hub)) is None:
                return 1
            use = prov.read_usage(acct, _now())           # the first answer was signed out
        if not st.usable:
            _say(f"{acct.id} is not a subscription login")
            return 1
        if st.plan and st.plan != acct.plan.name:
            acct = _with_detected_plan(acct, st.plan)
        if line := _expiry_lines({acct.id: st.expires_at} if st.expires_at else {},
                                 _now()).get(acct.id):
            _say(line)
    now = _now()
    why = _record_usage(acct, use, now)
    snaps = {acct.id: s} if (s := store.load(acct.id)) else {}
    if why:
        _say(_probe_line(acct.id, why, snaps.get(acct.id), now))
    v = rank([acct], snaps, now, tuning)[0]
    if v.eligible:
        _grace_warning(v, snaps, tuning, now)
    else:
        _say(f"warning: {acct.id} is near a limit: {v.reason}; {_usable(v, now)}")
    for line in _spent_models(acct.id, snaps.get(acct.id), tuning, now):
        _say(line)
    return _launch(v, _with_model(args, model, effort), explicit=True, continues=continues)


def _no_tool(then: str = "try again") -> bool:
    """Whether the provider's command is missing from PATH (said, ending with `then`): checked
    first by every command that starts a session or signs one in, before any sign-in check,
    probe or folder, so the cause is the one line shown."""
    prov = _provider()
    if prov.find_binary() is not None:
        return False
    _say(f"no {prov.tool} command found on PATH; install it, then {then}")
    return True


def cmd_run(a) -> int:
    if _no_tool():
        return 1
    if a.account:
        return _run_account(a.account, a.args)
    return _route(a.args, pause=True)


def _route(args, moving: str | None = None, pause: bool = False, model: str | None = None,
           effort: str | None = None, continues: str | None = None) -> int:
    """Start a session with `args` (as `_launch` takes them) on the best account (decision 11).
    `moving`: the account of a session picked up by request, named in one note when it could
    still take work. `pause`: on a terminal, a choice worth making waits `launch_pause` seconds
    for a key (`_pause`). `model`, `effort`: the session keeps them; an eligible account whose
    week for that model is spent is skipped, and when every eligible one is, a terminal asks
    whether to start the top one on its default model, and no terminal refuses. `continues` as
    `_launch` takes it."""
    try:
        while True:
            s = _survey()
            warned = False
            if moving and _can_work(s, moving):
                _say(f"note: {moving} can still take work; the session is picked up as asked")
                moving = None
            if s.out:
                _say("skipped " + ", ".join(f"{x.id} ({why})" for x, why in s.out))
            if signed_out := [x.id for x, why in s.out if why == NOT_SIGNED_IN]:
                _say("; ".join(f"`fairshare join {i}`" for i in signed_out)
                     + " signs " + ("it" if len(signed_out) == 1 else "them") + " in again")
            if fallback := [v.account.id for v in s.verdicts if v.account.id in s.failed]:
                _say(f"sign-in check failed for {', '.join(fallback)}; {FALLBACK}")
            for line in [*_probe_lines(s).values(), *_expiry_lines(s.expires, s.now).values()]:
                _say(line)
                warned = warned or line.startswith("warning:")
            if note := _preference_note(s):
                _say(note)
            _nothing_to_rank(s)
            if model and any(v.eligible for v in s.verdicts):
                having = _having_model(s, model)
                if having:
                    s = replace(s, verdicts=[v for v in s.verdicts
                                             if v in having or not v.eligible])
                elif not _interactive():
                    _say(f"{_no_model_line(model)}; name another with --model")
                    return 1
                elif (_say(_no_model_line(model)) or _choose(
                        f"[d] start on {s.verdicts[0].account.id} with its default model, "
                        "[q] quit: ", {"d", "q"})) != "d":
                    _say("not started")
                    return 0
                else:
                    model = effort = None
            top = s.verdicts[0]
            spent = _spent_models(top.account.id, s.snaps.get(top.account.id), s.tuning, s.now)
            if top.eligible:
                warned = _grace_warning(top, s.snaps, s.tuning, s.now) or warned
                for line in spent:
                    _say(line)
                if (pause and s.tuning.launch_pause > 0 and _interactive()
                        and _worth_pausing(s, top, warned)):
                    if (top := _pause(s, top)) is None:
                        _say("not started")
                        return 0
                return _launch(top, _with_model(args, model, effort), continues=continues)
            _say("no account is eligible:")
            for v in s.verdicts:
                print(f"  {v.account.id:<12} {v.reason}; {_usable(v, s.now)} (in {_in(v.usable_in)})"
                      f"{_age(s, v.account.id)}", file=sys.stderr)
            if not _interactive():
                return 75
            soon = min(s.verdicts, key=lambda v: (v.usable_in, v.account.id))
            print(f"[w]ait for {soon.account.id} ({_usable(soon, s.now)}), "
                  f"[s]tart on {top.account.id} now, [q]uit [q]: ", end="", file=sys.stderr)
            try:
                choice = input().strip().lower()[:1]
            except EOFError:
                choice = "q"
            if choice == "s":
                _say(f"starting on {top.account.id}, which is near a limit")
                for line in spent:
                    _say(line)
                return _launch(top, _with_model(args, model, effort), continues=continues)
            if choice != "w":
                return 75
            _say(f"waiting for {soon.account.id}, {_usable(soon, s.now)} (Ctrl-C to quit)")
            _wait_until(s.now + soon.usable_in * HOUR + 1)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def _find_session(text: str) -> SessionRecord | None:
    """The session record whose id is `text`, or the only one it starts, each taken at the end of
    its chain (`store.latest`: a session that went on under a new id is that one); None (said
    why, with the matches) otherwise."""
    recs = store.sessions()
    found = [r for r in recs if r.id == text] or [r for r in recs if r.id.startswith(text)]
    ends = dict.fromkeys(store.latest(r.id) for r in found)
    found = [r for i in ends if (r := store.session(i))]
    if len(found) == 1:
        return found[0]
    if not found:
        _say(f"no session recorded starts with {text!r}")
    else:
        _say(f"{text!r} matches {len(found)} sessions; give more of the id:")
        now = _now()
        for r in found:
            print(f"  {r.id}  {r.account:<12} {r.cwd}  (seen {_clock(r.seen, now)})",
                  file=sys.stderr)
    return None


def _unknown_account(account_id: str | None) -> bool:
    """Whether `-a` names no configured account (said, with the configured ids). Checked before
    a request goes to the launcher, which would otherwise stop the session for nothing."""
    if account_id is None:
        return False
    accounts = _load()[0]
    if any(x.id == account_id for x in accounts):
        return False
    _say(f"no account '{account_id}' (configured: {', '.join(x.id for x in accounts)})")
    return True


def _none_can_work(s: Survey) -> bool:
    """Whether no account can take work now; said, with the soonest one, when so."""
    if any(v.eligible for v in s.verdicts):
        return False
    soon = min(s.verdicts, key=lambda v: (v.usable_in, v.account.id), default=None)
    _say("no account can take work now"
         + (f"; the soonest is {soon.account.id}, {_usable(soon, s.now)}" if soon else "")
         + "; nothing done")
    return True


def cmd_pickup(a) -> int:
    """Continue a session on the best account (or `-a`), resumed under its own id with its
    model and effort (`--model`, `--effort` override them). Inside a session with no SESSION:
    this one; its launcher, when it has one, stops it and resumes it (a manual request: no check
    that the account is out), otherwise it resumes in the background. A SESSION (full id or
    unique prefix) from a terminal outside a session resumes in the foreground in its folder;
    inside a session or with --bg, in the background. Without `-a`, when no account can take
    work now, it says so (with the soonest) and does nothing, before any launcher is asked.
    fairshare never stops a session it did not start."""
    prov = _provider()
    if _no_tool():
        return 1
    if _unknown_account(a.account):
        return 2
    here = prov.current_session()
    if a.session is None:
        if here is None:
            _say("not inside a session; name one: fairshare pickup SESSION")
            return 2
        if (rec := store.session(here)) is None:
            _say(f"no record of this session ({here}) yet; it appears once its status line ran")
            return 1
        req = Request(rec.id, True, prov.session_pid(), PICKUP, a.model, a.effort, a.account)
        if launcher := _launcher():
            if not a.account and _none_can_work(_survey()):
                return 1
            _request(launcher, req)
            print("the launcher will stop this session and continue on the best account")
            return 0
        return _pickup_background(rec, req)
    if (rec := _find_session(a.session)) is None:
        return 2
    req = Request(rec.id, True, None, PICKUP, a.model, a.effort, a.account)
    if here or a.bg or not _interactive():
        return _pickup_background(rec, req)
    if not (rec.cwd and os.path.isdir(rec.cwd)):
        _say(f"the session's folder {rec.cwd} is gone; not picked up")
        return 1
    if not a.account and _none_can_work(_survey()):
        return 1
    os.chdir(rec.cwd)
    copy = _may_run(rec)
    return _start(rec, req, _resumed(rec, copy), moving=rec.account,
                  continues=rec.id + (COPY if copy else ""))


def _pickup_background(rec: SessionRecord, req: Request, fresh: bool = False) -> int:
    """Continue `rec` with no terminal, in its folder, on the request's account or the top
    eligible one that has its model (`_carry`) and trusts that folder or can get its trust by
    copy (nothing can ask); the tool's own output passes through. It resumes the session under
    its own id, or, when it was not stopped by its limit (it may still run, and fairshare stops
    no session it did not start), a copy under a new id with the record copied. `fresh`: a new
    session with the pickup prompt instead (a handoff)."""
    if not (rec.cwd and os.path.isdir(rec.cwd)):
        _say(f"the session's folder {rec.cwd} is gone; not picked up")
        return 1
    s = _survey()
    if not req.account and _none_can_work(s):
        return 1
    if not fresh and _can_work(s, rec.account):
        _say(f"note: {rec.account} can still take work; the session is picked up as asked")
    here = Path(rec.cwd).resolve()
    accounts, _, hub = _load()
    model, effort = _carry(rec, req)
    if req.account:
        candidates = [x for x in accounts if x.id == req.account]
        eligible = [v for v in s.verdicts if v.account.id == req.account]
    else:
        eligible = _having_model(s, model)
        if model and not eligible and any(v.eligible for v in s.verdicts):
            _say(f"{_no_model_line(model)}; name another with --model")
            return 1
        candidates = [v.account for v in eligible]
    acct = _start_here(candidates, here, accounts, hub)
    if acct is None:
        _say("no account can take it in the background: "
             + (f"eligible: {', '.join(v.account.id for v in eligible)}, none of which trusts "
                f"{here} ({_why_untrusted(here, accounts)}); or use `fairshare pickup "
                f"{rec.id}` from a terminal" if eligible else "none is eligible now"))
        return 1
    v = next((v for v in eligible if v.account.id == acct.id), None)
    _say(f"handing off {rec.id[:8]} to a fresh session on {acct.id}, in the background" if fresh
         else f"picking up {rec.id} on {acct.id}" + (f" ({v.reason})" if v else "")
         + ", in the background")
    prov, w = providers.get(acct.provider), _worker_of(rec.id)
    name = w.name if w else f"pickup-{rec.id[:8]}"
    extra = prov.model_args(model, effort)
    if fresh:
        return _start_background(acct, name, _pickup_prompt(rec, s.now, handed=True), rec.cwd,
                                 extra)
    fork = _may_run(rec)
    before = _live_named(acct, name)
    if code := _start_background(acct, name, _moved_prompt(rec, acct.id), rec.cwd,
                                 [*prov.resume_args(rec.id, fork), *extra]):
        return code
    new = _named_session(acct, name, before)
    if new == rec.id:                       # it went on under its own id
        new = None
    if new is None and not fork:
        store.move_session(rec.id, acct.id)
    elif new is None:
        print(f"the copy's session id is not known yet ({name}); fairshare finds it by name later"
              if w else f"the copy runs as {name}; its session id is not known yet")
    else:
        _copied(rec, new, acct, model, effort, w, fork)
        print(f"continued as session {new} on {acct.id}")
    return 0


def _live_named(acct: Account, name: str) -> set[str] | None:
    """The ids of the sessions running under `name` now, taken before a background start that
    is joined by that name (`_named_session`); None when the list cannot be had."""
    live = providers.get(acct.provider).list_sessions(acct)
    return None if live is None else {x.id for x in live if x.name == name}


def _named_session(acct: Account, name: str, before: set[str] | None) -> str | None:
    """The id of the session that a background start named `name` began: one listed under
    that name that was not among `before` (`_live_named`, taken before the start), looked for
    in the provider's list of running sessions for up to JOIN_WAIT seconds. None when nothing
    new is found, or when `before` could not be had: an earlier session of that name (an
    earlier pickup of the same session, a worker picked up twice) is never taken."""
    if before is None:
        return None
    end = _now() + JOIN_WAIT
    prov = providers.get(acct.provider)
    while True:
        live = prov.list_sessions(acct) or []
        if found := next((x.id for x in live if x.name == name and x.id not in before), None):
            return found
        if _now() >= end:
            return None
        _sleep(JOIN_POLL)


def _copied(rec: SessionRecord, new: str, acct: Account, model: str | None,
            effort: str | None, w: Worker | None, fork: bool) -> None:
    """A copy of `rec` runs as `new` on `acct`: link the two (`store.link_session`: role,
    parent, model and effort as carried, the checkpoint note copied; workers re-parented only
    when `rec` stopped, not for a `fork` of a session that may still run), and a worker's name
    now joins it."""
    store.link_session(rec.id, SessionSeen(
        new, rec.cwd, None, model, rec.model_name if model and model == rec.model else None,
        effort), acct.id, _now(), copy=fork)
    if w:
        store.join_worker(w.name, new)


# --- Handoff: a checkpoint, then a fresh session that reads it first (decision 23).

HANDOFF_NOTE_ASK = ("fairshare is handing this session over to a fresh session, which reads your "
                    "note first. Write that note now, as plain text: what was done, what is "
                    "next, the open decisions, and the files touched. Use no tools; answer with "
                    "the note alone.")
HANDOFF_WAIT_S = 300   # seconds the launcher waits for the handoff note before a pickup instead


def _write_checkpoint(session_id: str, text: str) -> Path:
    """Append `text` to the session's checkpoint note, dated, and clear its pending request."""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(_now()))
    path = store.append_checkpoint(session_id, f"## {stamp}\n{text}\n")
    store.update_session(session_id, lambda old: old and old.checkpoint_requested is not None
                         and replace(old, checkpoint_requested=None, request_said=None) or None)
    return path


def cmd_handoff(a) -> int:
    """Hand the current session over to a fresh session that reads its checkpoint first. With
    NOTE: the note is appended, then the launcher stops this session and starts the fresh one
    (without a launcher, in the background). Without NOTE (the user's `!`): the launcher stops
    this session, has it write the note in one headless turn, then starts the fresh one."""
    prov = _provider()
    if _no_tool():
        return 1
    if (here := prov.current_session()) is None:
        _say("not inside a session: handoff is for the session it runs in; to move another "
             "one: fairshare pickup SESSION")
        return 2
    if _unknown_account(a.account):
        return 2
    if (rec := store.session(here)) is None:
        _say(f"no record of this session ({here}) yet; it appears once its status line ran")
        return 1
    launcher = _launcher()
    req = Request(rec.id, True, prov.session_pid(), HANDOFF if a.note else HANDOFF_NOTE,
                  a.model, a.effort, a.account)
    if a.note:
        print(_write_checkpoint(rec.id, a.note))
        if launcher is None:
            if code := _pickup_background(rec, req, fresh=True):
                return code
            print("this session keeps running: stop here and leave the work to the new session")
            return 0
        _request(launcher, req)
        print("the launcher will stop this session and start a fresh one that reads the note first")
        return 0
    if launcher is None:
        _say("this session was not started by fairshare, so nothing can write its note while it "
             "waits; ask the session's agent instead: /fairshare handoff")
        return 1
    _request(launcher, req)
    print("the launcher will stop this session, have it write its handoff note, and start a "
          "fresh one that reads it first")
    return 0


def _write_note(rec: SessionRecord, req: Request) -> bool:
    """In the launcher, after it stopped its child: resume that session headless for one turn
    that writes its handoff note (no tools, its model and effort), on its own account when it
    can take work (its cache is warm), else the top eligible one; append the answer as its
    checkpoint. A status line shows each step (`_Status`); on a terminal, `c` skips. False,
    with one line why, when no note was written: the caller picks the session up instead."""
    status = _Status(_interactive())

    def fail(why: str) -> bool:
        status.clear()
        _say(f"no handoff note ({why}); picking the session up instead")
        return False
    status.show("checking accounts…")
    try:
        s = _drawing(status, _survey)
    except SystemExit as e:
        return fail(str(e))
    own = next((v.account for v in s.verdicts if v.account.id == rec.account), None)
    acct = own if own and _can_work(s, rec.account) else next(
        (v.account for v in s.verdicts if v.eligible), None)
    if acct is None:
        return fail("no account can take work")
    prov = providers.get(acct.provider)
    model, effort = _carry(rec, req)
    cmd, env = prov.note_command(acct, rec.id, prov.model_args(model, effort), HANDOFF_NOTE_ASK)
    try:
        proc = subprocess.Popen(cmd, env=_background_env(env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                cwd=rec.cwd if rec.cwd and os.path.isdir(rec.cwd) else None)
    except OSError as e:
        return fail(f"cannot start {cmd[0]}: {e.strerror}")
    try:
        how, text = _note_wait(proc, prov, acct.id, status)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
    if how != "done":
        return fail("skipped" if how == "skip" else f"no answer in {HANDOFF_WAIT_S} s")
    if proc.returncode or not text:
        return fail(f"the turn failed (exit {proc.returncode})" if proc.returncode
                    else "the answer held no note")
    path = _write_checkpoint(rec.id, text)
    status.clear()
    print(f"✓ note saved, {len(text.split())} words → {path}", file=sys.stderr, flush=True)
    return True


TICK_S = 0.1                              # the status line's redraw interval
SPIN_UTF8, SPIN_ASCII = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "|/-\\"
STATUS_PHASE_WIDTH = 44                   # the phase's column, so the clock stays put


def _width() -> int:
    try:
        return max(os.get_terminal_size(sys.stderr.fileno()).columns, 20)
    except (OSError, ValueError):
        return 80


class _Status:
    """One status line on stderr. On a terminal (`tty`): a spinner, the phase, the time since
    it began (m:ss) and a tail, redrawn in place by `draw`, cut to the terminal's width.
    Otherwise each phase once, as a plain line, as it starts."""

    def __init__(self, tty: bool):
        self.tty, self.start = tty, time.monotonic()
        self.phase = self.detail = self.tail = ""
        utf8 = (getattr(sys.stderr, "encoding", None) or "").lower().replace("-", "") == "utf8"
        self.frames = SPIN_UTF8 if utf8 else SPIN_ASCII

    def show(self, phase: str, detail: str = "", tail: str = "") -> None:
        """Show `phase` (with `detail` and `tail`, on a terminal only)."""
        if phase != self.phase and not self.tty:
            print(phase, file=sys.stderr, flush=True)
        self.phase, self.detail, self.tail = phase, detail, tail
        self.draw()

    def draw(self) -> None:
        if not self.tty:
            return
        t = time.monotonic() - self.start
        line = (f"{self.frames[int(t / TICK_S) % len(self.frames)]} "
                f"{self.phase + self.detail:<{STATUS_PHASE_WIDTH}} "
                f"{int(t) // 60}:{int(t) % 60:02d}{self.tail}")
        print(f"\r{line[:_width() - 1]}\x1b[K", end="", file=sys.stderr, flush=True)

    def clear(self) -> None:
        if self.tty:
            print("\r\x1b[K", end="", file=sys.stderr, flush=True)


def _drawing(status: _Status, fn):
    """`fn()` in a worker thread while this one redraws `status`: its result, or its exception
    (a SystemExit too)."""
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(fn)
        while True:
            try:
                return future.result(TICK_S)
            except TimeoutError:
                status.draw()


def _note_wait(proc: subprocess.Popen, prov, where: str, status: _Status,
               seconds: float | None = None) -> tuple[str, str | None]:
    """Wait for `proc`, the note turn, reading its output pipe as it comes (so it never fills)
    and showing its phase on `status`: reading the session on `where` until the model's first
    event, thinking, then writing the note with its words so far (`Provider.note_event`). The
    phase never goes back; a line that says nothing keeps it. "done" and the answer's text
    (`Provider.note_text` of the final line; None without one) when the turn ended; "skip"
    when `c` was typed (on a terminal: cbreak mode, always restored); "timeout" after `seconds`
    (HANDOFF_WAIT_S)."""
    end = time.monotonic() + (HANDOFF_WAIT_S if seconds is None else seconds)
    tty_fd = sys.stdin.fileno() if status.tty else None
    tail = " · c = skip" if tty_fd is not None else ""
    pipe, buf, last, phase, written = proc.stdout.fileno(), b"", "", 0, ""

    def show() -> None:
        status.show(*((f"reading the session on {where}…", ""), ("thinking…", ""),
                      ("writing the note", f" · {len(written.split())} words"))[phase], tail)

    def read(lines: list[bytes]) -> None:
        nonlocal last, phase, written
        for raw in lines:
            if not (line := raw.decode("utf-8", "replace").strip()):
                continue
            last, ev = line, prov.note_event(line)
            if ev and ev.kind == NOTE_THINKING:
                phase = max(phase, 1)
            elif ev and ev.kind == NOTE_WRITING:
                phase, written = 2, written + ev.text
    saved = None
    try:
        if tty_fd is not None:
            saved = termios.tcgetattr(tty_fd)
            tty.setcbreak(tty_fd)
            termios.tcflush(tty_fd, termios.TCIFLUSH)
        show()
        while not ((done := proc.poll() is not None) and pipe is None):
            if not done and (left := end - time.monotonic()) <= 0:
                return "timeout", None
            ready = select.select([f for f in (pipe, tty_fd) if f is not None], [], [],
                                  0 if done else min(left, TICK_S))[0]
            if pipe is not None and pipe in ready:
                if chunk := os.read(pipe, 65536):
                    *lines, buf = (buf + chunk).split(b"\n")
                    read(lines)
                else:                                 # the end of the output
                    pipe = None
            elif done:                                # ended, and its pipe held open elsewhere
                break
            if tty_fd is not None and tty_fd in ready and os.read(tty_fd, 1).lower() == b"c":
                return "skip", None
            show()
        read([buf])
        return "done", prov.note_text(last) if last else None
    finally:
        if saved is not None:
            termios.tcsetattr(tty_fd, termios.TCSADRAIN, saved)
        status.clear()


# --- Switch: bare `fairshare` inside a session started by the launcher (decision 23).

SWITCH_FLAGS = ("--model", "--effort", "-a", "--account")


def cmd_switch(a) -> int:
    """Inside a session: ask its launcher to stop it and start a fresh session on the best
    account (or `-a`) in the same folder, keeping its model and effort. Never a nested launch."""
    prov = _provider()
    here = prov.current_session()
    if (launcher := _launcher()) is None:
        _say("this session was not started by fairshare's launcher, so it cannot switch from "
             "inside; /exit, then fairshare")
        return 1
    if _unknown_account(a.account):
        return 2
    accounts = _load()[0]
    rec = store.session(here) or ((acct := _session_account(accounts)) and store.note_session(
        SessionSeen(here, os.getcwd()), acct.id, _now()))
    if rec is None:
        _say(f"no record of this session ({here}); nothing switched")
        return 1
    _request(launcher, Request(rec.id, True, prov.session_pid(), SWITCH, a.model, a.effort,
                               a.account))
    print("the launcher will stop this session and start a fresh one on the best account")
    return 0


def _background_env(env: dict[str, str]) -> dict[str, str]:
    """A background session's environment: no launcher of ours, and no BROWSER, which would not
    reach it anyway (the provider starts background sessions from its own pool): it uses the
    system browser."""
    return {k: v for k, v in env.items() if k not in (LAUNCHER_ENV, browser.ENV)}


def _start_background(acct: Account, name: str, prompt: str, cwd: str,
                      extra: list[str] = ()) -> int:
    """The start check on the account's hub folder, then a session with no terminal named
    `name`, in `cwd`, with `extra` arguments (a resume, a model), whose first message is
    `prompt`; the tool's own output passes through."""
    prov = providers.get(acct.provider)
    hub = acct.config_dir.parent.parent
    if not hub_mod.present(hub, acct.id):
        _say(_no_hub(hub))
        return 1
    try:
        for note in hub_mod.check(hub, acct.id, prov):
            _say(note)
    except OSError as e:
        _say(f"cannot prepare {acct.id}'s folder in the hub ({e}); not started")
        return 1
    cmd, env = prov.background(acct, name, prompt, list(extra))
    try:
        return subprocess.run(cmd, env=_background_env(env), cwd=cwd,
                              stdin=subprocess.DEVNULL).returncode
    except OSError as e:
        _say(f"cannot start {cmd[0]}: {e.strerror}")
        return 127 if isinstance(e, FileNotFoundError) else 126


# --- Folder trust without a terminal: copy, never create (decision 21).

def _copy_refused(here: Path) -> str | None:
    """Why trust for the canonical folder `here` may not be copied, else None: never in HOME
    (the provider keeps trust there per session), never where the user answered "only"."""
    if here == Path.home().resolve():
        return "fairshare writes no trust in HOME"
    if store.trust_only(here):
        return f"you answered \"only\" for {here} before, so fairshare copies no trust there"
    return None


def _trusting(accounts: list[Account], here: Path) -> list[Account]:
    """The joined accounts (with a plan) whose state trusts `here`."""
    return [a for a in accounts if a.plan is not None
            and providers.get(a.provider).trusts(a.config_dir, here) is True]


def _copy_trust(to: Account, source: Account, here: Path, hub: Path, tell) -> bool:
    """Write trust for `here` into `to`'s state, as the provider's own dialog would (lock,
    backup, read back), because `source` already trusts it. One line: through `tell` when
    written, on stderr when not."""
    try:
        key = providers.get(to.provider).trust(to.config_dir, here,
                                               hub_mod.backup_dir(hub, f"trust-{to.id}"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        _say(f"{to.id}: trust not written ({e})")
        return False
    tell(f"{to.id} now trusts {key} (copied from {source.id})")
    return True


def _start_here(candidates: list[Account], here: Path, accounts: list[Account],
                hub: Path) -> Account | None:
    """The first of `candidates` that trusts `here`, or that can get trust by copy (copied now,
    one line); None when none can."""
    refused, sources = _copy_refused(here), _trusting(accounts, here)
    for c in candidates:
        if any(t.id == c.id for t in sources):
            return c
        src = None if refused else next((t for t in sources if t.provider == c.provider), None)
        if src and _copy_trust(c, src, here, hub, _say):
            return c
    return None


def _why_untrusted(here: Path, accounts: list[Account]) -> str:
    """Why trust could not be had for `here` without a terminal."""
    if refused := _copy_refused(here):
        return refused
    if not _trusting(accounts, here):
        return ("no account trusts it, so there is nothing to copy: start a session there from a "
                "terminal and answer the trust question")
    return "copying trust failed"


def cmd_trust(a) -> int:
    """Copy folder trust for PATH (default: here) from an account that trusts it to every
    joined account that lacks it. Never creates trust, never in HOME, never where the user
    answered "only". Needs no terminal."""
    here = Path(a.path or os.getcwd()).expanduser().resolve()
    if not here.is_dir():
        _say(f"{here} is not a folder")
        return 2
    accounts, _, hub = _load()
    _require_hub(hub)
    if here == Path.home().resolve():
        _say(f"{here} is HOME: {_copy_refused(here)} (each session there asks for itself)")
        return 1
    sources = _trusting(accounts, here)
    lacking = [x for x in accounts if x.plan is not None and x not in sources]
    if not lacking:
        print(f"every account already trusts {here}; nothing to copy")
        return 0
    if refused := _copy_refused(here):
        _say(f"{refused}; nothing copied")
        return 1
    if not sources:
        _say(f"no account trusts {here}, and fairshare never creates trust: start a session "
             "there from a terminal (`fairshare`) and answer the question")
        return 1
    failed = 0
    for x in lacking:
        src = next((t for t in sources if t.provider == x.provider), None)
        if src is None:
            _say(f"{x.id}: no account of its provider trusts {here}; nothing copied")
            failed += 1
        elif not _copy_trust(x, src, here, hub, print):
            failed += 1
    return 1 if failed else 0


# --- Workers: background sessions a main session starts (decision 22).

WORKER_LINE = ("You are a worker for session {parent}. When fairshare asks for a checkpoint, run "
               'fairshare checkpoint "<what is done, what is next>".')
JOIN_WAIT = 10.0       # seconds a new worker's session id is looked for by its name
JOIN_POLL = 0.5        # seconds between looks
JOIN_RETRY_S = 3600    # a worker still unjoined this long after its start is no longer looked for


def _joined(w: Worker, session_id: str, now: float) -> None:
    """Note a worker's session id, and mark its session record as a worker's."""
    store.join_worker(w.name, session_id)
    store.update_session(session_id, lambda old: replace(
        old or SessionRecord(session_id, w.account, w.cwd, None, now),
        role=ROLE_WORKER, parent=w.parent))


def _join_pending(pending: list[Worker], accounts: list[Account], now: float) -> dict[str, str]:
    """Look the unjoined workers among `pending` up by name, with one listing of the running
    sessions; join each one found. {name: session id} of those joined."""
    todo = [w for w in pending if w.session is None and now - w.started < JOIN_RETRY_S]
    acct = next((a for w in todo for a in accounts if a.id == w.account), None)
    live = acct and providers.get(acct.provider).list_sessions(acct)
    found = {}
    for w in todo:
        if sid := next((x.id for x in live or [] if x.name == w.name), None):
            _joined(w, sid, now)
            found[w.name] = sid
    return found


def _new_worker(parent: str, acct: Account, cwd: str, name: str | None, model: str,
                effort: str) -> Worker | None:
    """Record a new worker under `name`, or under the first free `w-<parent's first 8>-<n>`;
    None when `name` is taken."""
    now = _now()
    for n in range(1, 10_000):
        w = Worker(name or f"w-{parent[:8]}-{n}", parent, acct.id, cwd, now, model=model,
                   effort=effort)
        if store.add_worker(w):
            return w
        if name:
            return None
    return None


def cmd_worker(a) -> int:
    """Start a background session for the calling session, on the model and effort the main
    session chose (`--effort none` for a model without one): on --account, else the top
    eligible account that has that model's week left and trusts this folder or can get its
    trust by copy; named, recorded, and joined to its session id by that name."""
    prov = _provider()
    if (parent := prov.current_session()) is None:
        _say("not inside a session: a worker belongs to the session that starts it")
        return 2
    if a.name is not None and not ACCOUNT_ID.fullmatch(a.name):
        _say(f"worker name {a.name!r}: use letters, digits, '.', '-' or '_'")
        return 2
    here = Path(os.getcwd()).resolve()
    accounts, _, hub = _load()
    _require_hub(hub)
    if a.account:
        chosen = next((x for x in accounts if x.id == a.account), None)
        if chosen is None:
            _say(f"no account '{a.account}' (configured: {', '.join(x.id for x in accounts)})")
            return 2
        if chosen.plan is None:
            _say(f"{chosen.id}: {chosen.plan_error}")
            return 1
        candidates, why = [chosen], ""
    else:
        s = _survey()
        if not any(v.eligible for v in s.verdicts):
            _say("no account can take work now; no worker started")
            return 1
        candidates = [v.account for v in _having_model(s, a.model)]
        if not candidates:
            _say(f"{_no_model_line(a.model)}; name another with --model; no worker started")
            return 1
        why = {v.account.id: f" ({v.reason})" for v in s.verdicts}
    acct = _start_here(candidates, here, accounts, hub)
    if acct is None:
        _say(f"{', '.join(c.id for c in candidates)} cannot work in {here}: "
             f"{_why_untrusted(here, accounts)}; no worker started")
        return 1
    w = _new_worker(parent, acct, str(here), a.name, a.model, a.effort)
    if w is None:
        _say(f"a worker named {a.name} is recorded already; choose another --name")
        return 2
    _say(f"starting worker {w.name} on {acct.id}{why and why[acct.id]}")
    prompt = WORKER_LINE.format(parent=parent) + "\n\n" + a.prompt
    before = _live_named(acct, w.name)
    if code := _start_background(acct, w.name, prompt, str(here),
                                 providers.get(acct.provider).model_args(a.model, a.effort)):
        store.drop_worker(w.name)
        return code
    if sid := _named_session(acct, w.name, before):
        _joined(w, sid, _now())
        print(f"worker {w.name} started on {acct.id} (session {sid[:8]})")
    else:
        print(f"worker {w.name} started on {acct.id}; its session id is not known yet "
              "(fairshare finds it by name later)")
    return 0


WORKER_STILL_S = 1800  # a worker whose cost has not moved this long has its cost recorded


def _measure(w: Worker, rec: SessionRecord | None) -> bool:
    """Record a worker's measured cost (its session's, as far as the series knows) and duration
    (its start to its last cost movement) on its worker record. Nothing reads them yet: data for
    sizing work later. False when there is no cost data or nothing changed."""
    if rec is None or not rec.costs:
        return False
    return store.finish_worker(w.name, store.total_cost(rec.costs),
                               max(0.0, rec.costs[-1][-1][0] - w.started))


def _measure_still(workers: list[Worker], now: float) -> None:
    """`_measure` each worker whose cost has not moved for WORKER_STILL_S."""
    for w in workers:
        if w.session and (rec := store.session(w.session)) and rec.costs \
                and now - rec.costs[-1][-1][0] >= WORKER_STILL_S:
            _measure(w, rec)


# --- Checkpoints: a session's own notes of where it stands, as files (decision 22).

def _target_workers(target: str, here: str | None) -> list[Worker] | None:
    """The workers `target` names: `all` (the calling session's), a worker's name, or its
    session id or the start of one. A session that went on under a new id counts as that one
    (`store.latest`). None (said why) when it names none."""
    if target == "all":
        if here is None:
            _say("`all` means this session's workers, and this is not inside a session")
            return None
        return store.workers(store.latest(here))
    if w := store.worker(target):
        return [w]
    ends = {store.latest(r.id) for r in store.sessions() if r.id.startswith(target)}
    found = [w for w in store.workers() if w.session
             and (w.session.startswith(target) or w.session in ends)]
    if len(found) != 1:
        _say(f"no worker is named {target!r} or has a session starting with it"
             if not found else f"{target!r} matches {len(found)} workers; give more of the id")
        return None
    return found


def _request_checkpoints(target: str, here: str | None) -> int:
    """Ask each named worker for a checkpoint: its hooks pass it on at its next event."""
    found = _target_workers(target, here)
    if found is None:
        return 2
    if not found:
        print("this session has no workers")
        return 0
    accounts, now, left = _load()[0], _now(), 0
    joined = _join_pending(found, accounts, now)
    for w in found:
        if (sid := w.session or joined.get(w.name)) is None:
            print(f"{w.name}: its session id is not known yet; not asked (try again shortly)")
            left += 1
            continue
        store.update_session(sid, lambda old: replace(
            old or SessionRecord(sid, w.account, w.cwd, None, now, role=ROLE_WORKER,
                                 parent=w.parent),
            checkpoint_requested=now, request_said=None))
        print(f"{w.name} (session {sid[:8]}): asked for a checkpoint")
    return 1 if left else 0


def _show_checkpoint(target: str) -> int:
    """Print the checkpoint note of a session (its id or start) or of a worker (its name)."""
    w = store.worker(target)
    ids = [w.session] if w and w.session else [i for i in store.checkpointed()
                                                 if i.startswith(target)]
    if len(ids) != 1 or not (path := store.checkpoint_path(ids[0])) or not path.is_file():
        _say(f"no checkpoint note for {target!r}" if len(ids) < 2
             else f"{target!r} matches {len(ids)} notes; give more of the id")
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_checkpoint(a) -> int:
    """`checkpoint TEXT`: append a note to this session's checkpoint file and clear its pending
    request. `--request X`: ask workers for one. `--show X`: print a session's note."""
    here = _provider().current_session()
    if a.request:
        return _request_checkpoints(a.request, here)
    if a.show:
        return _show_checkpoint(a.show)
    if not a.text:
        _say('say where you stand: fairshare checkpoint "<what is done, what is next>"')
        return 2
    if here is None:
        _say("not inside a session: a checkpoint belongs to the session that writes it")
        return 2
    print(_write_checkpoint(here, a.text))
    return 0


# --- `fairshare watch`: a main session waits on it as a background command (decision 22).

WATCH_LOCAL_S = 5.0    # seconds between looks at local state (snapshot, records, notes)
WATCH_LIST_S = 15.0    # seconds between listings of the running sessions (a subprocess)


def _watch_local(here: str, acct: Account | None, accounts: list[Account], t: Tuning,
                 now: float) -> list[str]:
    """What concerns the session `here` in local state: a new crossing of its account, and per
    worker a checkpoint written or a stop by its limit, since watch last said so. Said things
    are marked on the session's record."""
    events = []
    _measure_still(store.workers(here), now)
    if acct and acct.plan and (c := _tell_crossing(here, acct, t, now)):
        events.append(_warning_text(store.session(here), acct, c, accounts, t, now))
    found = []
    for w in store.workers(here):
        if not w.session:
            continue
        note = store.checkpoint_path(w.session)
        size = note.stat().st_size if note and note.is_file() else 0
        rec = store.session(w.session)
        found.append((w, note, size, rec and rec.stranded_at, rec and rec.account))
    resets = {}
    for x in {f[4] for f in found if f[3]}:
        a = next((a for a in accounts if a.id == x), None)
        if a and providers.get(a.provider).resumes_at_reset and (
                lim := _limit(a, store.load(x), t, now)):
            resets[x] = (f"resets {_clock(lim[1], now)} ({providers.get(a.provider).tool} "
                         "resumes it then); ")

    def change(old: SessionRecord | None) -> SessionRecord | None:
        if old is None:
            return None
        seen, said = dict(old.reported), []
        for w, note, size, stranded, account in found:
            if size > seen.get(f"checkpoint:{w.session}", 0):
                seen[f"checkpoint:{w.session}"] = size
                said.append(f"worker {w.name} wrote a checkpoint: {note}")
            if stranded and seen.get(f"stranded:{w.session}") != stranded:
                seen[f"stranded:{w.session}"] = stranded
                said.append(f"worker {w.name} was stopped by {account}'s limit: "
                            f"{resets.get(account, '')}fairshare pickup {w.session}")
        events.extend(said)
        return replace(old, reported=seen) if said else None
    store.update_session(here, change)
    return events


def _watch_live(here: str, accounts: list[Account], now: float) -> list[str]:
    """From the running sessions: each worker of `here` that ended (no longer listed) or went
    idle after it was seen busy, once each; pending joins are retried first."""
    mine = store.workers(here)
    if not mine:
        return []
    joined = _join_pending(mine, accounts, now)
    mine = [replace(w, session=w.session or joined.get(w.name)) for w in mine]
    acct = next((a for w in mine for a in accounts if a.id == w.account), None)
    live = acct and providers.get(acct.provider).list_sessions(acct)
    if live is None:
        return []
    state = {x.id: x.busy for x in live}
    events, ended_now = [], []

    def change(old: SessionRecord | None) -> SessionRecord | None:
        if old is None:
            return None
        seen, said = dict(old.reported), []
        for w in (w for w in mine if w.session):
            ended, busy = f"ended:{w.session}", f"busy:{w.session}"
            if w.session not in state:
                ended_now.append(w)
                if not seen.get(ended):
                    seen[ended] = 1
                    said.append(f"worker {w.name} ended")
                continue
            seen.pop(ended, None)
            if state[w.session]:
                seen[busy] = 1
            elif state[w.session] is False and seen.pop(busy, None):
                said.append(f"worker {w.name} went idle")
        events.extend(said)
        return replace(old, reported=seen) if seen != old.reported else None
    store.update_session(here, change)
    for w in ended_now:
        _measure(w, store.session(w.session))
    return events


def cmd_watch(a) -> int:
    """Block until something concerns the calling session, print one line per event and exit 0;
    `nothing happened` when --timeout runs out. Local state every WATCH_LOCAL_S seconds, the
    running sessions every WATCH_LIST_S. Writes only what it said (and joins) on the session's
    record, so a restarted watch does not repeat itself."""
    if (here := _provider().current_session()) is None:
        _say("not inside a session: watch waits for what concerns the session that runs it")
        return 2
    here = store.latest(here)
    accounts, tuning, _ = _load()
    start = _now()
    rec = store.session(here)
    acct = next((x for x in accounts if rec and x.id == rec.account), None) \
        or _session_account(accounts)
    if rec is None:
        rec = store.note_session(SessionSeen(here), acct.id if acct else "", start)
    next_list = start
    while True:
        now = _now()
        events = _watch_local(here, acct, accounts, tuning, now)
        if now >= next_list:
            events += _watch_live(here, accounts, now)
            next_list = now + WATCH_LIST_S
        if events:
            print("\n".join(events))
            return 0
        if a.timeout is not None and now >= start + a.timeout:
            print("nothing happened")
            return 0
        _sleep(WATCH_LOCAL_S if a.timeout is None else min(WATCH_LOCAL_S,
                                                            start + a.timeout - now))


def cmd_transcript(a) -> int:
    """The main conversation of a recorded session, oldest first: one block per message, the
    last N, each prefixed `user:` or `assistant:`. Never a raw record."""
    if (rec := _find_session(a.session)) is None:
        return 2
    if not rec.transcript:
        _say(f"no transcript recorded for {rec.id[:8]}")
        return 1
    said = _provider().transcript_text(Path(rec.transcript))
    if not said:
        _say(f"{rec.transcript}: could not be read, or its layout was not understood; "
             "nothing shown")
        return 1
    print("\n\n".join(_block(role, text) for role, text in said[-a.tail:]))
    return 0


OUTPUT_LINES = 20      # a command's output in `fairshare transcript` shows at most this many lines


def _block(role: str, text: str) -> str:
    """One message of `fairshare transcript`; a command the user ran and its output, shortened."""
    if role == "command":
        return f"user ran: {text}"
    if role == "output":
        lines = text.splitlines()
        more = len(lines) - OUTPUT_LINES
        return "output:\n" + "\n".join(lines[:OUTPUT_LINES]) + (
            f"\n… ({more} more lines)" if more > 0 else "")
    return f"{role}: {text}"


def _duration(text: str) -> float:
    """Seconds in `30m`, `3h`, `2d` (decimals allowed)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([mhd])", text.strip())
    if not m or float(m[1]) <= 0:
        raise SystemExit(f"fairshare: --for takes a length such as 30m, 3h or 2d, not '{text}'")
    return float(m[1]) * {"m": 60, "h": HOUR, "d": 24 * HOUR}[m[2]]


def _midnight(now: float) -> float:
    """The start of tomorrow, local time."""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def cmd_prefer(a) -> int:
    """Prefer one account for a while: launches, and later handoffs and briefings, go to it while
    it can take work. It ends on its own (default: the end of today), so it never pins."""
    now = _now()
    if a.off:
        store.clear_preference()
        print("no preferred account; the best account is used")
        return 0
    if a.id is None:
        pref = store.preference(now)
        print(f"preferring {pref[0]} until {_clock(pref[1], now)}" if pref
              else "no preferred account")
        return 0
    accounts = _load()[0]
    if a.id not in {x.id for x in accounts}:
        _say(f"no account '{a.id}' (configured: {', '.join(x.id for x in accounts)})")
        return 2
    until = now + _duration(a.duration) if a.duration else _midnight(now)
    store.save_preference(a.id, until)
    print(f"preferring {a.id} until {_clock(until, now)}"
          + ("" if a.duration else " (--for sets how long)")
          + "; `fairshare prefer --off` ends it")
    return 0


def _tilde(p: Path, home: Path) -> str:
    return f"~/{p.relative_to(home)}" if p.is_relative_to(home) and p != home else str(p)


def _yes(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


def _ask_yes(question: str) -> bool:
    """`_yes` on stderr's side of a launch; end of input is no."""
    try:
        print(f"fairshare: {question} [y/N] ", end="", file=sys.stderr)
        return input().strip().lower() == "y"
    except EOFError:
        return False


def _needs_terminal(command: str) -> int:
    _say(f"{command} asks questions and there is no terminal to answer them")
    return 2


def _by_provider(accounts: list[Account]):
    """(provider, its accounts) for each provider that has any."""
    for prov in providers.REGISTRY.values():
        if mine := [x for x in accounts if x.provider == prov.name]:
            yield prov, mine


def _provider():
    """The provider a new account joins. There is one; a second will need a flag on join."""
    (prov,) = providers.REGISTRY.values()
    return prov


def _read(p: Path) -> str | None:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _installed() -> str | None:
    """Absolute path of the installed `fairshare` command, as the shell would find it."""
    found = shutil.which("fairshare")
    return os.path.abspath(found) if found else None


def _need_installed(command: str) -> str:
    if not (installed := _installed()):
        raise SystemExit("fairshare: no installed `fairshare` found on PATH; install it first "
                         f"(pipx install fairshare-cli), then run `fairshare {command}` again")
    return installed


def _config_text(path: Path) -> str:
    """accounts.toml as text ("" when there is none). A file that cannot be read or parsed is
    refused, never taken for empty."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
        tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"fairshare: {path}: {e}; fix it or move it aside, then run this again")
    return text


def _edit_config(change) -> None:
    """Apply `change` (text -> text) to accounts.toml: under a lock, re-read just before writing,
    back up, write atomically, read back. Only the lines of the change differ."""
    path = store.config_path()
    with store.locked(path.with_name(path.name + ".lock")):
        old = _config_text(path)
        try:
            new = change(old)
        except ValueError as e:
            raise SystemExit(f"fairshare: {path}: {e}; nothing written")
        if new != old:
            backup = store.write_with_backup(path, new)
            print(f"Wrote {path}" + (f" (backup: {backup})" if backup else ""))


def _wire(prov, hub: Path, installed: str) -> str | None:
    """The hub's settings carry the usage hook: the installed fairshare by absolute path, then
    the user's own hook, if any (`--then`). What happened, or None when nothing changed."""
    return hub_mod.edit_shared(hub, prov.capture_file(hub_mod.shared_dir(hub)),
                               lambda text: prov.wire(text, shlex.quote(installed)), "settings")


def _wire_hub(prov, hub: Path, installed: str) -> None:
    if note := _wire(prov, hub, installed):
        print(f"usage hook: {note}")
    if note := _install_skill(prov, hub):
        print(f"skill: {note}")


def _skill_state(prov, hub: Path) -> tuple[Path, str, str | None, str]:
    """fairshare's skill in the hub: its place, the shipped text, what is there (None: nothing),
    and a word for it: current, missing, outdated or foreign (someone else's skill)."""
    dest, text = prov.skill(hub_mod.shared_dir(hub))
    there = _read(dest) if dest.exists() else None
    word = ("missing" if there is None else "current" if there == text
            else "outdated" if prov.own_skill(there) else "foreign")
    return dest, text, there, word


def _install_skill(prov, hub: Path) -> str | None:
    """Install fairshare's skill in the hub (one line), or refresh it (its diff shown first);
    another skill of that name is never touched. What happened ("installed in …", "updated …"),
    or None when it was current."""
    dest, text, there, word = _skill_state(prov, hub)
    if word == "current":
        return None
    if word == "foreign":
        return f"{dest} is not fairshare's skill; left alone"
    if there is not None:
        print(hub_mod.diff(prov, dest.name, there.encode(), text.encode(),
                           f"{dest.name} (fairshare's skill)").rstrip("\n"))

    def change(old: str | None) -> str | None:
        if old == text:
            return None
        if old is not None and not prov.own_skill(old):
            raise ValueError("not fairshare's skill")
        return text
    note = hub_mod.edit_shared(hub, dest, change, "skill")
    return f"installed in {dest}" if there is None and note == f"updated {dest}" else note


def _copy_state(prov, source: Path, account: Account, hub: Path) -> None:
    """After asking, carry over the few keys of the user's folder's state file that the
    provider allows (trust, user MCP servers, onboarding) into the new account's. The user's
    file is only read."""
    home = Path.home()
    src_path, dest = prov.state_file(source, home), prov.state_file(account.config_dir, home)
    if not src_path.exists():
        return
    try:
        src = json.loads(src_path.read_text(encoding="utf-8"))
        if not isinstance(src, dict):
            raise ValueError("not an object")
    except (OSError, UnicodeDecodeError, ValueError) as e:
        _say(f"{src_path} cannot be read ({e}); nothing copied from it")
        return

    def current() -> dict:
        if not dest.exists():
            return {}
        d = json.loads(dest.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return d

    shown = None
    try:
        while True:
            new, what = prov.merge_state(current(), src)
            if not what:
                return
            if what != shown:
                print(f"From {src_path}, {account.id} can take: {'; '.join(what)}.")
                if not _yes("Copy these?"):
                    print("  not copied")
                    return
                shown = what
            with prov.hold_state(dest):
                new, what = prov.merge_state(current(), src)
                if what != shown:
                    continue                     # it changed meanwhile: show it and ask again
                store.write_with_backup(dest, json.dumps(new, indent=2, ensure_ascii=False)
                                        + "\n", hub_mod.backup_dir(hub, account.id))
                if json.loads(dest.read_text(encoding="utf-8")) != new:
                    raise OSError(f"{dest} did not read back as written")
                print("  copied")
                return
    except (OSError, UnicodeDecodeError, ValueError) as e:
        _say(f"{dest}: {e}; nothing copied")


def _default_id(source: Path | None, taken: set[str], hub: Path) -> str:
    """The alias or folder name of `source` (as setup shows it), else the lowest free
    account-N. Never anything from the login."""
    if source is not None:
        for prov in providers.REGISTRY.values():
            for c in prov.discover(Path.home()):
                if c.config_dir.resolve() == source.resolve():
                    return c.id
        return source.name.lstrip(".")
    n = 1
    while f"account-{n}" in taken or hub_mod.account_dir(hub, f"account-{n}").exists():
        n += 1
    return f"account-{n}"


def _twins(acct: Account, st: AccountStatus, accounts: list[Account]) -> list[str]:
    """The other configured accounts signed in as the same login as `acct` (one quota)."""
    others = [a for a in accounts if a.provider == acct.provider and a.plan is not None
              and a.id != acct.id]
    return [a.id for a, s in zip(others, _probe(others))
            if s and st.identity and s.identity == st.identity]


def _resign(acct: Account, accounts: list[Account], hub: Path) -> AccountStatus | None:
    """Sign an account that is already joined in again: only the provider's own sign-in in its
    folder (with its browser), then the sign-in check. Nothing else changes. Its status when it
    is signed in as a subscription login, else None (said why)."""
    prov = providers.get(acct.provider)
    print(f"Signing in {acct.id} again: follow the sign-in the provider opens. Nothing else "
          "changes.")
    cmd, env = prov.login_command(acct.config_dir)
    if code := session.run_child(cmd, {**env, **_browser_env(hub, acct)}):
        _say(f"the sign-in did not finish (exit {code}); nothing changed")
        return None
    st = prov.account_status(acct)
    if st is None:
        _say(f"the sign-in check for {acct.id} gave no answer; `fairshare status` checks it again")
        return None
    store.record_subscription(acct.id, st.logged_in and st.usable, _now())
    if not (st.logged_in and st.usable):
        _say(f"{acct.id} is {'not a subscription login' if st.logged_in else 'not signed in'}")
        return None
    if twins := _twins(acct, st, accounts):
        _say(f"same login as {twins[0]}: one quota")
    print(f"{acct.id} is signed in")
    return st


def _ask_browser(account_id: str) -> str | None:
    """Which browser the account's sign-in opens in, as a `browser` line; None: the default."""
    profiles = browser.chrome_profiles(Path.home())
    picks = [None, *(f"{browser.CHROME}{d}" for d, _ in profiles), browser.COMMAND]
    print(f"Which browser should {account_id}'s sign-in links open in?")
    print("  1  the default browser")
    for i, (_, display) in enumerate(profiles, 2):
        print(f"  {i}  Chrome profile {display}")
    print(f"  {len(picks)}  a command (an executable that takes the link)")
    while True:
        answer = input("Choose [1]: ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(picks):
            break
    if (pick := picks[int(answer) - 1]) != browser.COMMAND:
        return pick
    while path := input("Absolute path of the command (Enter for the default browser): ").strip():
        try:
            browser.parse(browser.COMMAND + path)
            return browser.COMMAND + path
        except ValueError as e:
            print(e)
    return None


def _ask_name(where: str, default: str, taken: set[str], hub: Path) -> str:
    """A name for the account joined from `where`, asked until it is a valid id that no
    configured account and no hub folder has. The default may name a hub folder: join reuses it,
    as it does without the question."""
    while True:
        name = input(f"Name for {where} [{default}]: ").strip() or default
        if not ACCOUNT_ID.fullmatch(name):
            print(f"account id {name!r}: use letters, digits, '.', '-' or '_'")
        elif name in taken:
            print(f"{name} is already joined")
        elif name != default and hub_mod.account_dir(hub, name).exists():
            print(f"{name} is taken by a folder already in the hub")
        else:
            return name


def _same_login(account_id: str, twin: str, prov) -> str:
    """On a terminal, what to do with an account that signed in as `twin`'s login: "" (sign in
    again), "k" (keep anyway) or "n" (don't add)."""
    print(f"{account_id} signed in as the same account as {twin}, so they share one quota: "
          "keeping both adds nothing.")
    print(f"Usually the browser was still signed in to that account. Sign out on {prov.web}, "
          "or choose another browser profile, then:")
    while (choice := input("[Enter] sign in again · k = keep anyway · n = don't add "
                           ).strip().lower()) not in ("", "k", "n"):
        pass
    return choice


def _join(account_id: str | None, source: Path | None) -> int:
    """Create the account's folder in the hub with its links, run the provider's own sign-in in
    it, check who signed in, copy from the user's folder when asked, and add it to the config.
    An id already joined is only signed in again. With no id, a terminal is asked for one."""
    accounts, _, hub = _load()
    if account_id in {a.id for a in accounts}:
        acct = next(a for a in accounts if a.id == account_id)
        if source is not None:
            _say(f"{account_id} is already joined; --from is for a new account")
            return 2
        if not hub_mod.present(hub, account_id):
            _say(_no_hub(hub))
            return 1
        return 0 if _resign(acct, accounts, hub) else 1
    installed = _need_installed("join")
    prov = _provider()
    if source is not None:
        if not source.is_dir():
            _say(f"{source} is not a folder")
            return 2
        if source.resolve().is_relative_to(hub.resolve()):
            _say(f"{source} is inside the hub; name one of your own folders")
            return 2
    taken = {a.id for a in accounts}
    if account_id is None:
        account_id = _default_id(source, taken, hub)
        if _interactive():
            where = _tilde(source, Path.home()) if source is not None else "the new account"
            account_id = _ask_name(where, account_id, taken, hub)
    if not ACCOUNT_ID.fullmatch(account_id):
        _say(f"account id {account_id!r}: use letters, digits, '.', '-' or '_'")
        return 2
    if account_id in taken:
        _say(f"{account_id} is already joined (configured: {', '.join(sorted(taken))}); "
             f"`fairshare join {account_id}` signs it in again")
        return 2
    folder = hub_mod.account_dir(hub, account_id)
    if folder.exists():
        print(f"{account_id}: using the folder already in the hub, {folder}")
    for note in hub_mod.check(hub, account_id, prov):
        print(note)
    spec = _ask_browser(account_id) if _interactive() else None
    while True:                              # again while the login is another account's
        print(f"Signing in {account_id}: follow the sign-in the provider opens.")
        cmd, env = prov.login_command(folder)
        if code := session.run_child(cmd, {**env, **browser.env(hub, account_id, spec)}):
            _say(f"the sign-in did not finish (exit {code}); {account_id} not added; its folder "
                 f"stays at {folder}")
            return 1
        acct = Account(account_id, prov.name, load_plan(prov.default_plan), folder, browser=spec)
        st = prov.account_status(acct)
        if st is None:
            _say(f"the sign-in check for {account_id} gave no answer; it is added with plan "
                 f"{prov.default_plan}, and `fairshare status` checks it again")
            break
        store.record_subscription(account_id, st.logged_in and st.usable, _now())
        if not (st.logged_in and st.usable):
            _say(f"{account_id} is {'not a subscription login' if st.logged_in else 'not signed in'}"
                 f"; not added; its folder stays at {folder}")
            return 1
        if not (twins := _twins(acct, st, accounts)):
            break
        if not _interactive():
            print(f"same login as {twins[0]}: one quota")
            choice = "n"
        else:
            choice = _same_login(account_id, twins[0], prov)
        if choice == "k":
            break
        if choice == "n":
            print(f"{account_id} not added; its folder stays at {folder}")
            return 1
        spec = _ask_browser(account_id)
    if source is not None:
        settings = prov.capture_file(hub_mod.shared_dir(hub)).name
        prepare = {settings: lambda text: prov.wire(text, shlex.quote(installed)) or text}
        for note in hub_mod.seed(hub, source, account_id, prov, prepare):
            print(f"from {_tilde(source, Path.home())}: {note}")
    _wire_hub(prov, hub, installed)
    if source is not None:
        _copy_state(prov, source, acct, hub)
    plan = (st and st.plan) or prov.default_plan
    entry = {"id": account_id, "plan": plan, **({"browser": spec} if spec else {})}
    _edit_config(lambda text: with_account(text, entry))
    print(f"joined {account_id} ({plan})")
    return 0


def cmd_join(a) -> int:
    if _no_tool():
        return 1
    try:
        return _join(a.id, Path(a.source).expanduser() if a.source else None)
    except EOFError:
        return _needs_terminal("join")


def cmd_leave(a) -> int:
    """Stop routing to an account. Its folder and login stay; --logout signs it out first."""
    accounts, _, _ = _load()
    acct = next((x for x in accounts if x.id == a.id), None)
    if acct is None:
        _say(f"no account '{a.id}' (configured: {', '.join(x.id for x in accounts)})")
        return 2
    if a.logout:
        cmd, env = providers.get(acct.provider).logout_command(acct.config_dir)
        if code := session.run_child(cmd, env):
            _say(f"signing out {acct.id} failed (exit {code}); nothing changed")
            return 1
    _edit_config(lambda text: without_account(text, acct.id))
    print(f"left {acct.id}: no session is routed to it. Its folder"
          + (" stays" if a.logout else " and login stay") + f" at {acct.config_dir}")
    return 0


def _sign_in_labels(found, home: Path) -> list[str]:
    """What the sign-in check says of each of the user's own folders, all at once: "" when signed
    in with a subscription. A folder with no state file is not checked (the check would create
    one there): "sign-in unknown", as for a check that gives no answer."""
    accounts = [Account(c.id, c.provider, None, c.config_dir) for c in found]
    checked = [a for a in accounts
               if providers.get(a.provider).state_file(a.config_dir, home).exists()]
    answers = dict(zip((a.config_dir for a in checked), _probe(checked)))
    labels = []
    for a in accounts:
        st = answers.get(a.config_dir)
        labels.append("sign-in unknown" if st is None else "signed out" if not st.logged_in
                      else "not a subscription login" if not st.usable else "")
    return labels


def _setup() -> int:
    path, home = store.config_path(), Path.home()
    installed = _need_installed("setup")
    parsed = tomllib.loads(_config_text(path))
    if "hub" in parsed:
        print(f"Hub: {parsed['hub']}")
    else:
        hub_text = input(f"Hub folder (holds what every account shares) [{DEFAULT_HUB}]: ").strip()
        _edit_config(lambda text: with_hub(text, hub_text or DEFAULT_HUB))
    hub = _load()[2]
    hub_mod.accounts_dir(hub).mkdir(parents=True, exist_ok=True)    # a hub even with none joined
    if _no_tool("run `fairshare setup` again to join accounts"):
        for prov in providers.REGISTRY.values():
            _wire_hub(prov, hub, installed)
        return 1
    found = [c for prov in providers.REGISTRY.values() for c in prov.discover(home)
             if not c.config_dir.resolve().is_relative_to(hub.resolve())]
    print("Your own folders (fairshare copies from them and never changes them):" if found else
          "Found none of your own folders. Add an account with `fairshare join`.")
    joined = {a.id for a in _load()[0]}
    labels = _sign_in_labels(found, home)
    for i, (c, label) in enumerate(zip(found, labels), 1):
        print(f"  {i}  {c.id:<12} {_tilde(c.config_dir, home):<24} {c.found}"
              + (f"  ({label})" if label else "") + ("  (joined)" if c.id in joined else ""))
    # `all`: the signed-in folders first (the provider's default one leads discover's order)
    every = [c for c, label in sorted(zip(found, labels), key=lambda x: x[1] != "")]
    if found:
        seeded = dict.fromkeys(f for c in found for f in _seeded_files(providers.get(c.provider)))
        print(f"The first folder joined decides the shared {', '.join(seeded)}; "
              f"'all' joins in this order: {', '.join(c.id for c in every)}")
    answer = input("Join which? Numbers separated by spaces, 'all', or Enter for none: "
                   ) if found else ""
    words = answer.replace(",", " ").split()
    try:
        picked = every if words == ["all"] else [found[int(w) - 1] for w in words]
    except (ValueError, IndexError):
        _say(f"not a choice: {answer.strip()!r}; nothing joined")
        return 2
    for c in [c for c in picked if c.id in joined]:
        print(f"{c.id} is already joined; skipped (`fairshare join {c.id}` signs it in again)")
    failed = [c.id for c in picked if c.id not in joined and _join(None, c.config_dir) != 0]
    for prov in providers.REGISTRY.values():         # each join did this; also with none picked
        _wire_hub(prov, hub, installed)
    if failed:
        _say(f"not joined: {', '.join(failed)}")
    prov = _provider()
    print(f"start sessions with `fairshare`; plain `{prov.command}` still uses {prov.default_dir}, "
          "outside the hub")
    return 1 if failed else 0


def cmd_setup(_a) -> int:
    """Ask where the hub goes, then join each of the user's own folders they pick."""
    try:
        return _setup()
    except EOFError:
        return _needs_terminal("setup")


def _offer_setup() -> int:
    """Bare `fairshare` with no config: offer setup on a terminal, else say how."""
    how = f"no config at {store.config_path()}; run `fairshare setup` to set up your accounts"
    if not _interactive():
        _say(how)
        return 2
    try:
        if input("fairshare is not set up yet. Run `fairshare setup` now? [Y/n] "
                 ).strip().lower() in ("", "y", "yes"):
            return cmd_setup(None)
    except EOFError:
        pass
    _say(how)
    return 2


def _seeded_files(prov) -> list[str]:
    """The provider's shared files that the first folder copied decides (a later one's are only
    shown as a diff): neither folders nor append-only `.jsonl` logs."""
    return [i for i in prov.shared if not i.endswith("/") and not i.endswith(".jsonl")]


def _merge_back_prompt(prov, shared: Path, folders: list[str]) -> str:
    """What join copied in, as a prompt that copies it back: never what join never copies
    (`not_copied`) nor fairshare's own skill."""
    dirs = [i.rstrip("/") + "/" for i in prov.shared if i.endswith("/")
            and i not in prov.not_copied]
    logs = [i for i in prov.shared if i.endswith(".jsonl")]
    files = _seeded_files(prov)
    skill = prov.skill(shared)[0].parent.relative_to(shared).as_posix() + "/"
    return "\n".join([
        f"fairshare kept my {prov.name} history and settings in one shared folder, {shared}.",
        "Merge it back into my own config folders ("
        + (", ".join(folders) if folders else "ask me which") + ") without losing anything:",
        f"- folders ({', '.join(dirs)}): copy every file my folder lacks, except {skill}",
        "  (fairshare's own skill); when a file is in both with different content, keep both",
        "  and give the incoming one a new name;",
        f"- {', '.join(logs)}: append the lines my file lacks, in their order;",
        f"- {', '.join(files)}: show me the differences and ask before changing anything.",
        f"Never delete or change anything in {shared}, never open or copy a login file, and show",
        "me the plan and wait for my yes before you change anything."])


def cmd_uninstall(_a) -> int:
    """Changes nothing: says where everything is, how to remove fairshare, and gives a prompt
    for the user's own agent to merge the hub's history back into their own folders."""
    path, home = store.config_path(), Path.home()
    hub, accounts = None, []
    if path.exists():
        try:
            accounts, _, hub = load_accounts(path)
        except ConfigError as e:
            print(f"{path}: {e}")
    print("fairshare never changed your own folders, so there is nothing to undo in them.")
    print(f"Config: {path}" + ("" if path.exists() else " (none)"))
    if olds := sorted(p.name for p in path.parent.glob(path.name + ".bak-fairshare-*")):
        print(f"  backups of it, beside it: {', '.join(olds)}")
    print(f"State (usage snapshots, session records and checkpoint notes): {store.state_dir()}")
    if hub is not None:
        shared = hub_mod.shared_dir(hub)
        print(f"Hub: {hub}" + ("" if hub.exists() else " (not created)"))
        held = sorted(p.name + "/" * p.is_dir() for p in shared.iterdir()) if shared.is_dir() else []
        print(f"  shared: {', '.join(held) or 'nothing'}")
        configured = {a.id for a in accounts}
        folders = sorted(p.name for p in (hub / "accounts").iterdir()) \
            if (hub / "accounts").is_dir() else []
        print("  accounts: " + (", ".join(f"{i}{'' if i in configured else ' (left)'}"
                                          for i in folders) or "none"))
        if folders:
            print("  each stays signed in (on macOS, one keychain entry per folder) when the hub "
                  "is deleted; `fairshare leave --logout <id>` signs one out first")
        backups = hub / "backups"
        print(f"  backups: {len(list(backups.iterdir())) if backups.is_dir() else 0} "
              "folder(s) of things fairshare moved aside")
    print("To remove the installed fairshare: pipx uninstall fairshare-cli (or however you installed "
          "it). The config, the state and the hub stay until you delete them yourself.")
    if hub is not None:
        for prov in providers.REGISTRY.values():
            mine = [_tilde(c.config_dir, home) for c in prov.discover(home)]
            print("To bring the hub's history back into your own folders, give your own agent "
                  "this prompt:\n")
            print(_merge_back_prompt(prov, hub_mod.shared_dir(hub), mine))
    return 0


def _runs(runner: tuple[str, ...]) -> bool:
    """Whether the fairshare command `runner` starts and answers --help."""
    try:
        return subprocess.run([*runner, "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_doctor(a) -> int:
    """Read-only checks; with --fix, repairs first (one line each), then the checks. Exit 0 when
    everything ends fixed."""
    if a.hub and not a.fix:
        _say("--hub goes with --fix")
        return 2
    if not a.fix:
        return _doctor()
    fixed = _fix(Path(a.hub).expanduser() if a.hub else None)
    print()
    return 0 if _doctor() == 0 and fixed else 1


def _show(prov, item: str, old: str | None, new: str, name: str) -> None:
    print(hub_mod.diff(prov, item, (old or "").encode(), new.encode(), name).rstrip("\n"))


def _fix(hub_arg: Path | None) -> bool:
    """doctor --fix: find a moved hub (asking, or --hub), run the start check for every account,
    rewire a stale usage hook, offer to rename the old hub path in the shared settings, and after
    a move run the sign-in check. One line per fix; True when nothing is left to do."""
    left = []

    def report(ok: bool, text: str) -> None:
        print(f"{'fixed' if ok else 'FAIL '}  {text}")
        if not ok:
            left.append(text)

    path = store.config_path()
    try:
        accounts, _, hub = load_accounts(path)
    except (ConfigError, OSError) as e:
        report(False, f"{path.name}: {e}; fix it first")
        return False
    old = None
    if not hub_mod.present(hub):
        new = hub_arg
        if new is None and _interactive():
            while True:
                try:
                    answer = input(f"No hub at {hub}. Where is it now? (Enter to stop) ").strip()
                except EOFError:
                    break
                if not answer:
                    break
                if hub_mod.present(Path(answer).expanduser()):
                    new = Path(answer).expanduser()
                    break
                print(f"{answer} holds no shared/ and accounts/ folders")
        if new is None:
            report(False, f"no hub at {hub}; run `fairshare doctor --fix --hub PATH` with where "
                          "it is now")
            return False
        if not hub_mod.present(new):
            report(False, f"{new} holds no shared/ and accounts/ folders; nothing changed")
            return False
        new = Path(os.path.abspath(new))
        _edit_config(lambda text: with_hub(text, str(new)))
        report(True, f"hub: now {new} (was {hub})")
        old, hub = hub, new
        accounts = _load()[0]
    elif hub_arg is not None and Path(os.path.abspath(hub_arg)) != hub:
        print(f"note   the hub at {hub} is there; --hub {hub_arg} not used")
    for prov, mine in _by_provider(accounts):
        for acct in mine:
            try:
                for note in hub_mod.check(hub, acct.id, prov):
                    report(True, note)
            except OSError as e:
                report(False, f"{acct.id}: cannot repair its folder in the hub ({e})")
    installed = _installed()
    for prov, _mine in _by_provider(accounts):
        settings = prov.capture_file(hub_mod.shared_dir(hub))
        text = _read(settings)
        if installed is None:
            report(False, "no installed `fairshare` on PATH; the usage hook cannot be rewired")
        else:
            try:
                if new_text := prov.wire(text, shlex.quote(installed)):
                    _show(prov, settings.name, text, new_text, f"{settings.name} (rewired)")
                    note = _wire(prov, hub, installed)
                    report(note is not None and note.startswith("updated"),
                           f"usage hook: {note or 'unchanged'}")
            except ValueError as e:
                report(False, f"usage hook: {settings} {e}; left as it is")
        if (_skill_state(prov, hub)[3]) != "current":
            note = _install_skill(prov, hub)
            report(bool(note and note.startswith(("updated", "installed"))), f"skill: {note}")
        if old is not None:
            text = _read(settings)
            try:
                new_text, places = prov.repath(text, str(old), str(hub)) if text else (None, [])
            except ValueError as e:
                report(False, f"{settings}: {e}; it may still name the old hub")
                continue
            if not places:
                continue
            _show(prov, settings.name, text, new_text, f"{settings.name} (old hub path renamed)")
            yes = _interactive() and _ask_yes(f"{settings.name} names the old hub in "
                                              f"{', '.join(places)}. Rename it?")
            note = yes and hub_mod.edit_shared(hub, settings,
                                               lambda t: prov.repath(t, str(old), str(hub))[0],
                                               "settings")
            if note and note.startswith("updated"):
                report(True, f"old hub path renamed in {', '.join(places)}: {note}")
            else:
                report(False, f"{settings.name} still names the old hub in {', '.join(places)}"
                       + (f" ({note})" if note else ""))
    if old is not None:                   # a moved login may read as signed out (keychain entries)
        planned = [a for a in accounts if a.plan is not None and a.provider in providers.REGISTRY]
        for a, st in zip(planned, _probe(planned)):
            if st is not None and not st.logged_in:
                report(False, f"{a.id} is signed out after the move; `fairshare join {a.id}` "
                              "signs it in again")
    return not left


def _usage_check(a: Account, use: dict[str, Window] | str | None) -> str:
    """doctor's line for one account's usage probe: what it read (names and a count, never a
    value), why it failed, or that it is off."""
    if use is None:
        return "usage probe off (usage_args = [])"
    if isinstance(use, str):
        return f"usage probe failed ({use})"
    plain = sorted(k for k in use if not window_model(k))
    models = len(use) - len(plain)
    read = [*plain, *([f"{models} model week{'s' * (models != 1)}"] if models else [])]
    return (f"usage probe answers ({', '.join(read)}), "
            + ("local command" if a.usage_args is None else "with usage_args"))


def _doctor() -> int:
    """One line per check, read-only. Safe to paste: account ids and plan names only."""
    failed = []

    def check(ok: bool, text: str) -> None:
        print(f"{'pass' if ok else 'FAIL'}  {text}")
        if not ok:
            failed.append(text)

    for prov in providers.REGISTRY.values():
        found = prov.find_binary() is not None
        check(found, f"{prov.name}: command " + (f"found, version {prov.version() or 'unknown'}"
                                                 if found else "not found on PATH"))
    accounts: list[Account] = []
    hub = None
    path = store.config_path()
    if not path.exists():
        check(False, "accounts.toml not found (run `fairshare setup`)")
    else:
        try:
            accounts, _, hub = load_accounts(path)
        except ConfigError as e:
            check(False, f"{path.name}: {e} (fix it, or move it aside and run `fairshare setup`)")
    if hub is not None and not hub_mod.present(hub):
        check(False, "no hub at the configured path; if you moved it, run `fairshare doctor --fix`")
        accounts = []                 # a sign-in check would create folders at the old path
    checkable = [a for a in accounts if a.plan is not None and a.provider in providers.REGISTRY]
    answers = iter(_check(checkable, _now()))
    for a in accounts:
        if a.plan is None:
            check(False, f"{a.id}: {a.plan_error}")
        elif a.provider not in providers.REGISTRY:
            check(False, f"{a.id}: unknown provider {a.provider}")
        else:
            st, use = next(answers)
            until = (f" until {time.strftime('%Y-%m-%d', time.localtime(st.expires_at))}"
                     if st and st.expires_at else "")
            how = ("answers: " + ("signed out" if not st.logged_in else
                                  "not a subscription login" if not st.usable else
                                  f"signed in{until}, plan {st.plan or a.plan.name}")
                   ) if st else "gave no answer"
            check(st is not None, f"{a.id}: sign-in check {how}")
            check(not isinstance(use, str), f"{a.id}: {_usage_check(a, use)}")
            check(*(lambda ok, text: (ok, f"{a.id}: {text}"))(
                *providers.get(a.provider).check_state(a.config_dir)))
    callers: dict[tuple[str, ...], list[str]] = {}
    for prov, _mine in _by_provider(accounts):
        text = _read(prov.capture_file(hub_mod.shared_dir(hub)))
        cap = prov.wired(text)
        check(cap is not None, f"{prov.name}: usage hook in the hub "
              + ("calls fairshare capture" if cap else "does not call fairshare capture "
                                                       "(run `fairshare setup`)"))
        if cap:
            callers.setdefault(cap.runner, []).append(prov.name)
        runner = prov.hooked(text)
        check(runner is not None, f"{prov.name}: hooks in the hub ({', '.join(HOOKS)}) "
              + ("call fairshare hook" if runner else "do not all call fairshare hook "
                                                     "(run `fairshare doctor --fix`)"))
        if runner and runner not in callers:
            callers[runner] = []
        where, _, _, word = _skill_state(prov, hub)
        check(word == "current", f"{prov.name}: skill in the hub is " + {
            "current": "current", "missing": "missing (run `fairshare doctor --fix`)",
            "outdated": "outdated (run `fairshare doctor --fix`)",
            "foreign": f"not fairshare's: {where.parent.name}/ holds another skill"}[word])
    if not callers and (installed := _installed()):
        callers[(installed,)] = []
    if not callers:
        check(False, "fairshare command not found on PATH")
    for runner, names in callers.items():
        who = f"called by the {', '.join(names)} usage hook" if names else "installed"
        check(_runs(runner), f"fairshare command ({who}) runs")
    return 1 if failed else 0


def _positive(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"a whole number of 1 or more, not {text!r}")
    return int(text)


def _carry_flags(p: argparse.ArgumentParser) -> None:
    """The flags of every verb that starts a session in place of another (D3)."""
    p.add_argument("--model", help="start on this model (default: the session's latest)")
    p.add_argument("--effort", help="at this effort level (default: the session's latest)")
    p.add_argument("-a", "--account", metavar="ID", help="on this account, skipping the ranking")


def _run_args(parser: argparse.ArgumentParser, rest: list[str]) -> argparse.Namespace:
    """`run [-a ID | --account ID] [--] ARGS...`. Only a leading -a/--account is fairshare's;
    everything else goes to the provider unchanged (it takes a prompt as its first argument, so a
    positional id or argparse's own option handling would not do)."""
    account = None
    if rest[:1] in (["-a"], ["--account"]):
        if len(rest) < 2:
            parser.error("run: --account needs an id")
        account, rest = rest[1], rest[2:]
    elif rest and rest[0].startswith("--account="):
        account, rest = rest[0].partition("=")[2], rest[1:]
    return argparse.Namespace(account=account, args=rest[1:] if rest[:1] == ["--"] else rest)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fairshare", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(f=cmd_status)
    cp = sub.add_parser("capture", help="the usage hook; the account is the session's own folder")
    cp.add_argument("account", nargs="?", help=argparse.SUPPRESS)   # the form before the hub
    cp.add_argument("--then", help="the user's own command for this hook, run after recording")
    cp.set_defaults(f=cmd_capture)
    sub.add_parser("run", help="start a session on the best account (also: no command at all); "
                   "`run [-a ID] [--] ARGS...` passes ARGS to the provider unchanged")
    pp = sub.add_parser("prefer", help="prefer one account for a while (default: the rest of "
                        "today); with no id, show the current preference")
    pp.add_argument("id", nargs="?")
    pp.add_argument("--for", dest="duration", metavar="LENGTH", help="e.g. 30m, 3h, 2d")
    pp.add_argument("--off", action="store_true", help="end the preference now")
    pp.set_defaults(f=cmd_prefer)
    sub.add_parser("setup", help="choose the hub and join your own folders"
                   ).set_defaults(f=cmd_setup)
    jp = sub.add_parser("join", help="add an account: sign in to it in its own hub folder")
    jp.add_argument("id", nargs="?", help="default: the --from folder's alias or name, "
                                          "else account-N")
    jp.add_argument("--from", dest="source", metavar="FOLDER",
                    help="copy (never move) history and settings from one of your own folders")
    jp.set_defaults(f=cmd_join)
    lp = sub.add_parser("leave", help="stop routing to an account; its folder and login stay")
    lp.add_argument("id")
    lp.add_argument("--logout", action="store_true", help="also sign that account out")
    lp.set_defaults(f=cmd_leave)
    sub.add_parser("uninstall", help="say how to remove fairshare; changes nothing"
                   ).set_defaults(f=cmd_uninstall)
    kp = sub.add_parser("pickup", help="continue a session on the best account (inside a "
                        "session: this one)")
    kp.add_argument("session", nargs="?", metavar="SESSION", help="a session id or its start")
    kp.add_argument("--bg", action="store_true", help="in the background, with no terminal")
    _carry_flags(kp)
    kp.set_defaults(f=cmd_pickup)
    hq = sub.add_parser("handoff", help="inside a session: a checkpoint, then a fresh session "
                        "that reads it first (with no NOTE, the launcher has the session write it)")
    hq.add_argument("note", nargs="?", metavar="NOTE", help="what was done, what is next, open "
                    "decisions, files touched")
    _carry_flags(hq)
    hq.set_defaults(f=cmd_handoff)
    tp = sub.add_parser("transcript", help="show what a recorded session said, as text")
    tp.add_argument("session", metavar="SESSION", help="a session id or its start")
    tp.add_argument("--tail", type=_positive, default=40, metavar="N",
                    help="the last N messages (default 40)")
    tp.set_defaults(f=cmd_transcript)
    sp = sub.add_parser("trust", help="copy folder trust from an account that has it to the "
                        "accounts that lack it (never creates trust)")
    sp.add_argument("path", nargs="?", metavar="PATH", help="default: the current folder")
    sp.set_defaults(f=cmd_trust)
    wp = sub.add_parser("worker", help="inside a session: start a background worker session")
    wp.add_argument("--account", metavar="ID", help="default: the best account")
    wp.add_argument("--name", help="default: w-<first 8 of this session>-<n>")
    wp.add_argument("--model", required=True, help="the worker's model, fitted to its task")
    wp.add_argument("--effort", required=True, help="its effort level; `none` for a model "
                    "without one")
    wp.add_argument("prompt", metavar="PROMPT")
    wp.set_defaults(f=cmd_worker)
    cq = sub.add_parser("checkpoint", help="inside a session: note where it stands; "
                        "--request asks workers for one, --show prints a note")
    cq.add_argument("text", nargs="?", metavar="TEXT")
    one = cq.add_mutually_exclusive_group()
    one.add_argument("--request", metavar="SESSION|NAME|all",
                     help="ask a worker (or all of this session's) for a checkpoint")
    one.add_argument("--show", metavar="SESSION|NAME", help="print a session's checkpoint note")
    cq.set_defaults(f=cmd_checkpoint)
    ap = sub.add_parser("watch", help="inside a session: wait until something concerns it "
                        "(its account's limit, its workers), then say what")
    ap.add_argument("--timeout", type=_positive, metavar="SECONDS",
                    help="give up after this long: `nothing happened`")
    ap.set_defaults(f=cmd_watch)
    rp = sub.add_parser("probe", help=argparse.SUPPRESS)  # capture starts it in the background
    rp.add_argument("id")
    rp.set_defaults(f=cmd_probe)
    hp = sub.add_parser("hook", help=argparse.SUPPRESS)   # fairshare's own hooks call it
    hp.add_argument("name")
    hp.set_defaults(f=cmd_hook)
    dp = sub.add_parser("doctor", help="check the setup, read-only; --fix repairs what it can")
    dp.add_argument("--fix", action="store_true", help="repair: find a moved hub, relink, rewire")
    dp.add_argument("--hub", metavar="PATH", help="with --fix: where the hub is now")
    dp.set_defaults(f=cmd_doctor)
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv and not store.config_path().exists():
        return _offer_setup()
    if (not argv or argv[0] in SWITCH_FLAGS) and _provider().current_session():
        sw = argparse.ArgumentParser(prog="fairshare", description="inside a session: stop it and "
                                     "start a fresh one on the best account (a switch)")
        _carry_flags(sw)
        return cmd_switch(sw.parse_args(argv))
    if not argv or argv[0] == "run":
        return cmd_run(_run_args(p, argv[1:]))
    if argv[0] not in sub.choices and argv[0] not in ("-h", "--help"):
        _say(f"'{argv[0]}' is not a command; did you mean: fairshare run {shlex.join(argv)}")
        return 2
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
