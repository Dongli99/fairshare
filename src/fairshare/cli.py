"""fairshare CLI: status | capture | run (the default) | prefer | setup | join | leave | uninstall |
doctor."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from . import browser
from . import hub as hub_mod
from . import providers, session, store
from .model import (ACCOUNT_ID, DEFAULT_HUB, Account, AccountStatus, ConfigError, Snapshot, Tuning,
                    load_accounts, load_plan, with_account, with_hub, without_account)
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
    snaps = {a.id: s for a in accounts if (s := store.load(a.id))}
    live, twins, notes, failed = [], [], {}, []
    out = [(a, a.plan_error) for a in accounts if a.plan is None]     # no plan file: not ranked
    planned = [a for a in accounts if a.plan is not None]
    for a, st in zip(planned, _probe(planned)):
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
            live.append((a, st))
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
    return Survey(verdicts, out, twins, notes, failed, snaps, tuning, now, pref)


def _in(h: float | None) -> str:
    return "-" if h is None else f"{int(h)}h{int(h % 1 * 60):02d}m"


def _clock(t: float, now: float) -> str:
    """A local clock time, with the day name when it is not today. Unlike "in 2h", it stays true
    on a line that is not redrawn."""
    lt = time.localtime(t)
    return time.strftime("%H:%M" if lt[:3] == time.localtime(now)[:3] else "%a %H:%M", lt)


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
    for i, v in enumerate(s.verdicts):
        mark = ">" if i == 0 else " "
        flag = "" if v.eligible else f"  [avoid] {_usable(v, s.now)}"
        if i == 0 and _honoured(s):
            flag = f"  [preferred until {_clock(s.preference[1], s.now)}]"
        print(f"{mark} {v.account.id:<12} {v.account.plan.name:<14} score {v.score:6.2f}  "
              f"{v.reason}{flag}")
        for note in s.notes.get(v.account.id, []):
            print(f"    {note}")
    for a, why in s.out:
        print(f"  {a.id:<12} {a.plan.name if a.plan else '-':<14} left out: {why}")
        for note in s.notes.get(a.id, []):
            print(f"    {note}")
    for a, rep in s.twins:
        print(f"  {a.id:<12} {a.plan.name:<14} same login as {rep}")
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


def _status_line(acct: Account, snap: Snapshot | None, now: float, live: bool,
                 t: Tuning) -> str:
    """The account and its windows, shortest first, named by their length (`5h 23%`). A window
    at the hot mark turns yellow, red once spent, and shows when it resets: a tight account
    about to reset is still worth using. A window the plan has but whose usage is unknown (none
    seen yet, or reset since: nothing is known until the next exchange) shows `-`, so the line
    keeps its shape. A session's first redraws carry no usage yet, so the saved snapshot stands
    in, marked with when it was taken."""
    p = acct.plan
    hours = {p.short_name: p.short_hours, p.long_name: p.long_hours} if p else {}
    known = {k: w for k, w in (snap.windows if snap else {}).items() if w.resets_at > now}
    names = [k for k in hours if k] + [k for k in known if k not in hours]
    parts = [acct.id]
    for k in sorted(names, key=lambda k: hours.get(k) or 0):
        label, w = _span(hours[k]) if hours.get(k) else k, known.get(k)
        if w is None:
            parts.append(f"{label} -")
            continue
        text = f"{label} {w.used_pct:.0f}%"
        if w.used_pct >= t.short_hot_pct:
            colour = SPENT if w.used_pct >= t.long_exhausted_pct else TIGHT
            text = f"{colour}{text} ({_clock(w.resets_at, now)}){PLAIN}"
        parts.append(text)
    if known and not live:
        parts.append(f"as of {_clock(snap.captured_at, now)}")
    return " · ".join(parts)


def cmd_capture(a) -> int:
    """Runs as the provider's usage hook. Must never raise or print noise. The account is the
    session's own folder in the hub; a session outside the hub records nothing. With --then, the
    user's own command for that hook gets the same stdin and prints the line. An account id after
    `capture` (the form before the hub) is ignored."""
    raw = sys.stdin.buffer.read()
    try:
        accounts, tuning, _ = _load()
        acct = _session_account(accounts)
        fresh = acct and providers.get(acct.provider).parse_capture(json.loads(raw))
        now = time.time()
        snap = store.record(acct.id, fresh, now) if fresh else acct and store.load(acct.id)
        if acct and not a.then:
            print(_status_line(acct, snap, now, bool(fresh), tuning))
    except (Exception, SystemExit):      # whatever is wrong, the user's own line must still run
        pass
    if a.then:
        sys.stdout.flush()
        return subprocess.run(a.then, shell=True, input=raw).returncode
    return 0


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
    browser), and return its exit code."""
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
    return session.run_child(cmd + args, env)


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
    st = providers.get(acct.provider).account_status(acct)
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
        if not st.usable:
            _say(f"{acct.id} is not a subscription login")
            return 1
        if st.plan and st.plan != acct.plan.name:
            acct = _with_detected_plan(acct, st.plan)
    now = _now()
    snaps = {acct.id: s} if (s := store.load(acct.id)) else {}
    v = rank([acct], snaps, now, tuning)[0]
    if v.eligible:
        _grace_warning(v, snaps, tuning, now)
    else:
        _say(f"warning: {acct.id} is near a limit: {v.reason}; {_usable(v, now)}")
    return _launch(v, args, explicit=True)


def cmd_run(a) -> int:
    if a.account:
        return _run_account(a.account, a.args)
    try:
        while True:
            s = _survey()
            if s.out:
                _say("skipped " + ", ".join(f"{x.id} ({why})" for x, why in s.out))
            if signed_out := [x.id for x, why in s.out if why == NOT_SIGNED_IN]:
                _say("; ".join(f"`fairshare join {i}`" for i in signed_out)
                     + " signs " + ("it" if len(signed_out) == 1 else "them") + " in again")
            if fallback := [v.account.id for v in s.verdicts if v.account.id in s.failed]:
                _say(f"sign-in check failed for {', '.join(fallback)}; {FALLBACK}")
            if note := _preference_note(s):
                _say(note)
            _nothing_to_rank(s)
            top = s.verdicts[0]
            if top.eligible:
                _grace_warning(top, s.snaps, s.tuning, s.now)
                return _launch(top, a.args)
            _say("no account is eligible:")
            for v in s.verdicts:
                print(f"  {v.account.id:<12} {v.reason}; {_usable(v, s.now)} (in {_in(v.usable_in)})",
                      file=sys.stderr)
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
                return _launch(top, a.args)
            if choice != "w":
                return 75
            _say(f"waiting for {soon.account.id}, {_usable(soon, s.now)} (Ctrl-C to quit)")
            _wait_until(s.now + soon.usable_in * HOUR + 1)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


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
        elif (cap := prov.wired(text)) is None or cap.runner != (installed,):
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
    answers = iter(_probe(checkable))
    for a in accounts:
        if a.plan is None:
            check(False, f"{a.id}: {a.plan_error}")
        elif a.provider not in providers.REGISTRY:
            check(False, f"{a.id}: unknown provider {a.provider}")
        else:
            st = next(answers)
            how = ("answers: " + ("signed out" if not st.logged_in else
                                  "not a subscription login" if not st.usable else
                                  f"signed in, plan {st.plan or a.plan.name}")
                   ) if st else "gave no answer"
            check(st is not None, f"{a.id}: sign-in check {how}")
            check(*(lambda ok, text: (ok, f"{a.id}: {text}"))(
                *providers.get(a.provider).check_state(a.config_dir)))
    callers: dict[tuple[str, ...], list[str]] = {}
    for prov, _mine in _by_provider(accounts):
        cap = prov.wired(_read(prov.capture_file(hub_mod.shared_dir(hub))))
        check(cap is not None, f"{prov.name}: usage hook in the hub "
              + ("calls fairshare capture" if cap else "does not call fairshare capture "
                                                       "(run `fairshare setup`)"))
        if cap:
            callers.setdefault(cap.runner, []).append(prov.name)
    if not callers and (installed := _installed()):
        callers[(installed,)] = []
    if not callers:
        check(False, "fairshare command not found on PATH")
    for runner, names in callers.items():
        who = f"called by the {', '.join(names)} usage hook" if names else "installed"
        check(_runs(runner), f"fairshare command ({who}) runs")
    return 1 if failed else 0


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
