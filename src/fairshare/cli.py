"""fairshare CLI: status | capture | run (the default) | prefer | pickup | transcript | setup |
join | leave | uninstall | doctor."""
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
from .model import (ACCOUNT_ID, DEFAULT_HUB, USAGE_MODEL_TURN, Account, AccountStatus, ConfigError,
                    SessionRecord, Snapshot, Tuning, Window, load_accounts, load_plan,
                    window_model, with_account, with_hub, without_account)
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


def cmd_status(_a) -> int:
    s = _survey()
    if s.preference and not _honoured(s):
        print(_preference_note(s))
    probes, expiring = _probe_lines(s), _expiry_lines(s.expires, s.now)
    for i, v in enumerate(s.verdicts):
        mark, aid = (">" if i == 0 else " "), v.account.id
        flag = "" if v.eligible else f"  [avoid] {_usable(v, s.now)}"
        if i == 0 and _honoured(s):
            flag = f"  [preferred until {_clock(s.preference[1], s.now)}]"
        print(f"{mark} {aid:<12} {v.account.plan.name:<14} score {v.score:6.2f}  "
              f"{v.reason}{_age(s, aid)}{flag}")
        for note in [*s.notes.get(aid, []), *([probes[aid]] if aid in probes else []),
                     *_spent_models(aid, s.snaps.get(aid), s.tuning, s.now),
                     *([expiring[aid]] if aid in expiring else [])]:
            print(f"    {note}")
    for a, why in s.out:
        print(f"  {a.id:<12} {a.plan.name if a.plan else '-':<14} left out: {why}")
        for note in s.notes.get(a.id, []):
            print(f"    {note}")
    for a, rep in s.twins:
        print(f"  {a.id:<12} {a.plan.name:<14} same login as {rep}")
        if a.id in expiring:
            print(f"    {expiring[a.id]}")
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
        snap = store.record(acct.id, fresh, now) if fresh else acct and store.load(acct.id)
        seen = acct and prov.session_of(payload)
        if seen:
            with contextlib.suppress(OSError):
                out = _stranded(acct, seen, snap, tuning, now)
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


def _spent_now(acct: Account, snap: Snapshot | None, t: Tuning, now: float) -> bool:
    """Whether one of the account's plan windows is spent and not yet reset. The policy calls a
    full short window usable inside its grace hour; for a session the provider already stopped,
    that is still out: it should be picked up, not wait for the reset."""
    p = acct.plan
    return bool(p and snap) and any(
        w.used_pct >= t.long_exhausted_pct and w.resets_at > now
        for k, w in snap.windows.items() if k in (p.short_name, p.long_name))


def _stranded(acct: Account, seen, snap: Snapshot | None, t: Tuning, now: float) -> str | None:
    """Record the session; the pickup hint when its limit stopped it and its account still
    cannot take work by the policy. Numbers newer than the stop that say it can work again (a
    window reset) clear the mark."""
    rec = store.note_session(seen, acct.id, now)
    if rec is None or rec.stranded_at is None or acct.plan is None:
        return None
    if (_spent_now(acct, snap, t, now)
            or not rank([acct], {acct.id: snap} if snap else {}, now, t)[0].eligible):
        return PICKUP_MARK
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
    hook's input on stdin. Never raises, prints nothing, exits 0: the provider may ignore or show
    whatever a hook says."""
    def work():
        raw = json.loads(sys.stdin.buffer.read())
        if handle := HOOKS.get(a.name):
            handle(a.name, raw)
    return _quietly(work)


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


def _request(launcher: int, session_id: str, manual: bool, pid: int | None) -> None:
    """Ask the launcher to stop its child and continue the session elsewhere."""
    store.save_request(launcher, session_id, manual, pid)
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
    store.note_session(seen, acct.id, now, stranded=True)
    _start_probe(acct, seen.cwd, now)
    if launcher := _launcher():
        _request(launcher, seen.id, False, prov.session_pid())


STOP_FAILURE = "stop-failure"
HOOKS = {STOP_FAILURE: _stop_failure}   # fairshare's hook name -> handler(name, input)


def _say(text: str) -> None:
    print(f"fairshare: {text}", file=sys.stderr)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _grace_warning(v: Verdict, snaps: dict[str, Snapshot], tuning: Tuning, now: float) -> None:
    """An account that is eligible only because its hot short window resets soon."""
    w = (snap := snaps.get(v.account.id)) and snap.windows.get(v.account.plan.short_name)
    if w and w.used_pct >= tuning.short_hot_pct and 0 < w.resets_at - now:
        left = (w.resets_at - now) / HOUR
        if left < tuning.grace_hours:
            _say(f"warning: {v.account.id} {v.account.plan.short_name} window "
                 f"{w.used_pct:.0f}% used, resets in {int(left * 60)}m")


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


def _launch(v: Verdict, args: list[str], explicit: bool = False) -> int:
    """Check the hub is there, offer folder trust, run the start check on the account's hub
    folder, then start the provider as a child, in its clean environment (with the account's
    browser and LAUNCHER_ENV naming this process), and return its exit code. A pickup request
    (SIGUSR1, `_pickup_request`) stops the child and continues its session on the best account,
    with this process still the parent, so a later limit picks up again."""
    prov, acct = providers.get(v.account.provider), v.account
    hub = acct.config_dir.parent.parent
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
    env = {**env, **_browser_env(hub, acct)}
    if dropped := prov.dropped_login_vars():
        _say(f"ignoring {', '.join(dropped)} from the environment; the account's own login is used")
    if passed := prov.passed_session_vars():
        _say(f"passing {', '.join(passed)} through from the surrounding session")
    asked: list[tuple[SessionRecord, Survey | None]] = []

    def requested(child: int) -> bool:
        if got := _pickup_request(child):
            asked.append(got)
        return bool(got)
    code = session.run_child(cmd + args, {**env, LAUNCHER_ENV: str(os.getpid())}, requested)
    if code != session.PICKUP:
        return code
    return _continue(*asked[-1])


COUNTDOWN_S = 10       # seconds the launcher shows before an automatic pickup starts


def _can_work(s: Survey, acct_id: str) -> bool:
    """Whether the account (or the twin ranked for its login) can take work in the survey. A plan
    window spent right now says no even inside the policy's grace hour: the provider is already
    refusing, so a session stopped by it should not wait for the reset."""
    rep = next((r for a, r in s.twins if a.id == acct_id), acct_id)
    v = next((v for v in s.verdicts if v.account.id == rep and v.eligible), None)
    return v is not None and not _spent_now(v.account, s.snaps.get(rep), s.tuning, s.now)


def _pickup_request(child: int) -> tuple[SessionRecord, Survey | None] | None:
    """Called in the launcher's wait on SIGUSR1. The request for this launcher, when it is for
    its own child's session and stands: a manual one (`fairshare pickup`) as it is; an automatic
    one only when the session is marked stranded and a fresh survey (probes included) confirms
    its account cannot take work. Otherwise None: back to waiting, nothing printed (the session
    owns the terminal)."""
    try:
        got = store.take_request(os.getpid())
        if got is None or (got[2] is not None and got[2] != child):
            return None
        rec = store.session(got[0])
        if rec is None or got[1]:
            return (rec, None) if rec else None
        if rec.stranded_at is None:
            return None
        s = _survey()
        return None if _can_work(s, rec.account) else (rec, s)
    except (Exception, SystemExit):
        return None


def _limit_text(s: Survey, acct_id: str) -> str:
    """Which window stopped the account and when it resets, for the countdown line."""
    v = next((v for v in s.verdicts if v.account.id == acct_id), None)
    if v is None:
        return next((why for a, why in s.out if a.id == acct_id), "no usage known")
    p, snap = v.account.plan, s.snaps.get(acct_id)
    for name, hours, full in ((p.long_name, p.long_hours, s.tuning.long_exhausted_pct),
                              (p.short_name, p.short_hours, s.tuning.short_hot_pct)):
        w = name and snap and snap.windows.get(name)
        if w and w.resets_at > s.now and w.used_pct >= full:
            return f"{_span(hours) if hours else name} resets {_clock(w.resets_at, s.now)}"
    return _usable(v, s.now)


def _countdown(text, seconds: int = COUNTDOWN_S) -> bool:
    """On the terminal: `text(seconds left)` on one line, counting down. False when the user
    types `c`; True on Enter or when the time is up. Keys are read one by one (cbreak mode, what
    was typed before is dropped) and the terminal is always restored."""
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
                        return True
                    if key in (b"c", b"C"):
                        return False
        return True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print(file=sys.stderr)


def _pickup_prompt(rec: SessionRecord, now: float) -> str:
    """The first prompt of the session that continues `rec`. Provider-neutral. `first` is the
    one place a step that comes before the rest goes."""
    if rec.stranded_at is not None:
        why = (f"which stopped at {_clock(rec.stranded_at, now)} because account {rec.account} "
               "hit its usage limit")
    else:
        why = f"which was stopped at {_clock(now, now)} on account {rec.account} to move it"
    first: list[str] = []
    return " ".join([
        f"This session continues session {rec.id}, {why}. Its folder: {rec.cwd}.", *first,
        f"Run `fairshare transcript {rec.id} --tail 40` to see where it stopped; read the full "
        f"transcript at {rec.transcript} only if something is unclear. Then continue the work. "
        "First say in one line what you are picking up."])


def _continue(rec: SessionRecord, s: Survey | None) -> int:
    """After the launcher stopped its child: for an automatic pickup on a terminal, a countdown
    (cancel says how to resume later); then the pickup through the normal launch path, in the
    session's folder, with the pickup prompt as the first prompt."""
    if s is not None and _interactive():
        best = next((v.account.id for v in s.verdicts if v.eligible), None)
        where = f"on {best}" if best else "(no account can take work now)"
        if not _countdown(lambda left: f"{rec.account} hit its limit ({_limit_text(s, rec.account)}"
                          f"). Continuing {where} in {left} s · Enter = now · c = cancel"):
            prov = next((providers.get(a.provider) for a in _load()[0] if a.id == rec.account),
                        _provider())
            _say(f"not continued; to resume it later: fairshare run -a {rec.account} -- "
                 f"{shlex.join(prov.resume_args(rec.id))}")
            return 0
    if rec.cwd and os.path.isdir(rec.cwd):
        os.chdir(rec.cwd)
    return _route([_pickup_prompt(rec, _now())], moving=None if s else rec.account)


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


def _run_account(account_id: str, args: list[str]) -> int:
    """The user's explicit choice: no ranking, no refusal. Only an account that is not signed in
    with a subscription is stopped; a signed-out one is offered a sign-in on a terminal."""
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
    return _launch(v, args, explicit=True)


def cmd_run(a) -> int:
    if a.account:
        return _run_account(a.account, a.args)
    return _route(a.args)


def _route(args: list[str], moving: str | None = None) -> int:
    """Start a session with `args` on the best account (decision 11). `moving`: the account of a
    session picked up by request, named in one note when it could still take work."""
    try:
        while True:
            s = _survey()
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
            if note := _preference_note(s):
                _say(note)
            _nothing_to_rank(s)
            top = s.verdicts[0]
            spent = _spent_models(top.account.id, s.snaps.get(top.account.id), s.tuning, s.now)
            if top.eligible:
                _grace_warning(top, s.snaps, s.tuning, s.now)
                for line in spent:
                    _say(line)
                return _launch(top, args)
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
                return _launch(top, args)
            if choice != "w":
                return 75
            _say(f"waiting for {soon.account.id}, {_usable(soon, s.now)} (Ctrl-C to quit)")
            _wait_until(s.now + soon.usable_in * HOUR + 1)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def _find_session(text: str) -> SessionRecord | None:
    """The session record whose id is `text`, or the only one it starts; None (said why, with the
    matches) otherwise."""
    recs = store.sessions()
    found = [r for r in recs if r.id == text] or [r for r in recs if r.id.startswith(text)]
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


def cmd_pickup(a) -> int:
    """Continue a session on the best account. Inside a session with no SESSION: this one; its
    launcher, when it has one, stops it and continues (a manual request: no check that the
    account is out), otherwise a background session takes it over. A SESSION (full id or unique
    prefix) from a terminal outside a session starts in the foreground in its folder; inside a
    session or with --bg, in the background. fairshare never stops a session it did not start."""
    prov = _provider()
    here = prov.current_session()
    if a.session is None:
        if here is None:
            _say("not inside a session; name one: fairshare pickup SESSION")
            return 2
        if (rec := store.session(here)) is None:
            _say(f"no record of this session ({here}) yet; it appears once its status line ran")
            return 1
        if launcher := _launcher():
            _request(launcher, rec.id, True, prov.session_pid())
            print("the launcher will stop this session and continue on the best account")
            return 0
        return _pickup_background(rec)
    if (rec := _find_session(a.session)) is None:
        return 2
    if here or a.bg or not _interactive():
        return _pickup_background(rec)
    if not (rec.cwd and os.path.isdir(rec.cwd)):
        _say(f"the session's folder {rec.cwd} is gone; not picked up")
        return 1
    os.chdir(rec.cwd)
    return _route([_pickup_prompt(rec, _now())], moving=rec.account)


def _pickup_background(rec: SessionRecord) -> int:
    """Start the pickup with no terminal, in the session's folder, on the top eligible account
    that already trusts that folder (nothing can ask), and pass the tool's own output through."""
    if not (rec.cwd and os.path.isdir(rec.cwd)):
        _say(f"the session's folder {rec.cwd} is gone; not picked up")
        return 1
    s = _survey()
    if _can_work(s, rec.account):
        _say(f"note: {rec.account} can still take work; the session is picked up as asked")
    here = Path(rec.cwd).resolve()
    eligible = [v for v in s.verdicts if v.eligible]
    trusting = [v for v in eligible
                if providers.get(v.account.provider).trusts(v.account.config_dir, here) is True]
    if not trusting:
        _say("no account can take it in the background: "
             + (f"eligible: {', '.join(v.account.id for v in eligible)}, none of which trusts "
                f"{here} (start it once from a terminal there, or use `fairshare pickup "
                f"{rec.id}` from a terminal)" if eligible else "none is eligible now"))
        return 1
    v = trusting[0]
    acct, prov = v.account, providers.get(v.account.provider)
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
    cmd, env = prov.background(acct, f"pickup-{rec.id[:8]}", _pickup_prompt(rec, s.now))
    env = {k: v for k, v in {**env, **_browser_env(hub, acct)}.items() if k != LAUNCHER_ENV}
    _say(f"picking up {rec.id} on {acct.id} ({v.reason}), in the background")
    try:
        return subprocess.run(cmd, env=env, cwd=rec.cwd, stdin=subprocess.DEVNULL).returncode
    except OSError as e:
        _say(f"cannot start {cmd[0]}: {e.strerror}")
        return 127 if isinstance(e, FileNotFoundError) else 126


def cmd_transcript(a) -> int:
    """The main conversation of a recorded session, oldest first: one block per message, the
    last N, each prefixed `user:` or `assistant:`. Never a raw record."""
    if (rec := _find_session(a.session)) is None:
        return 2
    path = Path(rec.transcript) if rec.transcript else None
    said = path and _provider().transcript_text(path)
    if not said:
        _say(f"{rec.transcript}: could not be read, or its layout was not understood; "
             "nothing shown")
        return 1
    print("\n\n".join(f"{role}: {text}" for role, text in said[-a.tail:]))
    return 0


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
                         f"(pipx install .), then run `fairshare {command}` again")
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


def _join(account_id: str | None, source: Path | None) -> int:
    """Create the account's folder in the hub with its links, run the provider's own sign-in in
    it, check who signed in, copy from the user's folder when asked, and add it to the config.
    An id already joined is only signed in again."""
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
    account_id = account_id or _default_id(source, taken, hub)
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
    print(f"Signing in {account_id}: follow the sign-in the provider opens.")
    cmd, env = prov.login_command(folder)
    if code := session.run_child(cmd, {**env, **browser.env(hub, account_id, spec)}):
        _say(f"the sign-in did not finish (exit {code}); {account_id} not added; its folder "
             f"stays at {folder}")
        return 1
    acct = Account(account_id, prov.name, load_plan(prov.default_plan), folder, browser=spec)
    st = prov.account_status(acct)
    if st is not None:
        store.record_subscription(account_id, st.logged_in and st.usable, _now())
        if not (st.logged_in and st.usable):
            _say(f"{account_id} is {'not a subscription login' if st.logged_in else 'not signed in'}"
                 f"; not added; its folder stays at {folder}")
            return 1
        if twins := _twins(acct, st, accounts):
            print(f"same login as {twins[0]}: one quota")
            if not _yes(f"Keep {account_id} anyway?"):
                print(f"{account_id} not added; its folder stays at {folder}")
                return 1
    else:
        _say(f"the sign-in check for {account_id} gave no answer; it is added with plan "
             f"{prov.default_plan}, and `fairshare status` checks it again")
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
    found = [c for prov in providers.REGISTRY.values() for c in prov.discover(home)
             if not c.config_dir.resolve().is_relative_to(hub.resolve())]
    print("Your own folders (fairshare copies from them and never changes them):" if found else
          "Found none of your own folders. Add an account with `fairshare join`.")
    joined = {a.id for a in _load()[0]}
    for i, (c, label) in enumerate(zip(found, _sign_in_labels(found, home)), 1):
        print(f"  {i}  {c.id:<12} {_tilde(c.config_dir, home):<24} {c.found}"
              + (f"  ({label})" if label else "") + ("  (joined)" if c.id in joined else ""))
    answer = input("Join which? Numbers separated by spaces, 'all', or Enter for none: "
                   ) if found else ""
    words = answer.replace(",", " ").split()
    try:
        picked = found if words == ["all"] else [found[int(w) - 1] for w in words]
    except (ValueError, IndexError):
        _say(f"not a choice: {answer.strip()!r}; nothing joined")
        return 2
    for c in [c for c in picked if c.id in joined]:
        print(f"{c.id} is already joined; skipped (`fairshare join {c.id}` signs it in again)")
    failed = [c.id for c in picked if c.id not in joined and _join(c.id, c.config_dir) != 0]
    for prov in providers.REGISTRY.values():         # each join did this; also with none picked
        _wire_hub(prov, hub, installed)
    if failed:
        _say(f"not joined: {', '.join(failed)}")
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


def _merge_back_prompt(prov, shared: Path, folders: list[str]) -> str:
    dirs = [i.rstrip("/") + "/" for i in prov.shared if i.endswith("/")]
    logs = [i for i in prov.shared if i.endswith(".jsonl")]
    files = [i for i in prov.shared if not i.endswith("/") and not i.endswith(".jsonl")]
    return "\n".join([
        f"fairshare kept my {prov.name} history and settings in one shared folder, {shared}.",
        "Merge it back into my own config folders ("
        + (", ".join(folders) if folders else "ask me which") + ") without losing anything:",
        f"- folders ({', '.join(dirs)}): copy every file my folder lacks; when a file is in both",
        "  with different content, keep both and give the incoming one a new name;",
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
    print(f"State (usage snapshots): {store.state_dir()}")
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
        backups = hub / "backups"
        print(f"  backups: {len(list(backups.iterdir())) if backups.is_dir() else 0} "
              "folder(s) of things fairshare moved aside")
    print("To remove the installed fairshare: pipx uninstall fairshare (or however you installed "
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
        check(runner is not None, f"{prov.name}: stop-failure hook in the hub "
              + ("calls fairshare hook" if runner else "does not call fairshare hook "
                                                      "(run `fairshare doctor --fix`)"))
        if runner and runner not in callers:
            callers[runner] = []
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
    kp.set_defaults(f=cmd_pickup)
    tp = sub.add_parser("transcript", help="show what a recorded session said, as text")
    tp.add_argument("session", metavar="SESSION", help="a session id or its start")
    tp.add_argument("--tail", type=_positive, default=40, metavar="N",
                    help="the last N messages (default 40)")
    tp.set_defaults(f=cmd_transcript)
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
    if not argv or argv[0] == "run":
        return cmd_run(_run_args(p, argv[1:]))
    if argv[0] not in sub.choices and argv[0] not in ("-h", "--help"):
        _say(f"'{argv[0]}' is not a command; did you mean: fairshare run {shlex.join(argv)}")
        return 2
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
