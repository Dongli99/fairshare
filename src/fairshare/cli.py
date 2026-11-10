"""fairshare CLI: status | pick | capture | run (the default) | init | apply | uninstall | doctor."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from . import providers, session, store
from .model import (Account, AccountStatus, Snapshot, Tuning, load_accounts, load_plan,
                    plan_names, render_config)
from .policy import HOUR, Verdict, rank


def _load():
    path = store.config_path()
    if not path.exists():
        raise SystemExit(f"fairshare: no config at {path} (see `fairshare init`)")
    return load_accounts(path)


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


def _survey() -> Survey:
    """Check every account once, then rank those that can take work."""
    accounts, tuning = _load()
    now = _now()
    snaps = {a.id: s for a in accounts if (s := store.load(a.id))}
    live, out, twins, notes, failed = [], [], [], {}, []
    for a, st in zip(accounts, _probe(accounts)):
        if st is None:
            failed.append(a.id)
            signed_in = providers.get(a.provider).login_present(a)
            how = "found, counted as signed in" if signed_in else "not found, counted as signed out"
            notes[a.id] = [f"sign-in check failed; login file {how}; plan {a.plan.name} from config"]
            if signed_in:
                live.append((a, None))
            else:
                out.append((a, "not signed in"))
        elif not st.logged_in:
            out.append((a, "not signed in"))
        elif not st.usable:
            out.append((a, "not a subscription login"))
        else:
            if st.plan and st.plan != a.plan.name:
                notes[a.id] = [f"plan {a.plan.name} in config, detected {st.plan}"]
                a = replace(a, plan=load_plan(st.plan))
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
    return Survey(rank(ranked, snaps, now, tuning), out, twins, notes, failed, snaps, tuning, now)


def _in(h: float | None) -> str:
    return "-" if h is None else f"{int(h)}h{int(h % 1 * 60):02d}m"


def _usable(v: Verdict, now: float) -> str:
    """When a spent account can take work again, as a local clock time; a day name when that
    is not today."""
    t = time.localtime(now + v.usable_in * HOUR)
    same_day = t[:3] == time.localtime(now)[:3]
    return f"usable at {time.strftime('%H:%M' if same_day else '%a %H:%M', t)}"


def cmd_status(_a) -> int:
    s = _survey()
    for i, v in enumerate(s.verdicts):
        mark = ">" if i == 0 else " "
        flag = "" if v.eligible else f"  [avoid] {_usable(v, s.now)}"
        print(f"{mark} {v.account.id:<12} {v.account.plan.name:<14} score {v.score:6.2f}  "
              f"{v.reason}{flag}")
        for note in s.notes.get(v.account.id, []):
            print(f"    {note}")
    for a, why in s.out:
        print(f"  {a.id:<12} {a.plan.name:<14} left out: {why}")
        for note in s.notes.get(a.id, []):
            print(f"    {note}")
    for a, rep in s.twins:
        print(f"  {a.id:<12} {a.plan.name:<14} same login as {rep}")
    return 0


def _nothing_to_rank(s: Survey) -> None:
    if not s.verdicts:
        why = "; ".join(f"{a.id}: {w}" for a, w in s.out)
        raise SystemExit(f"fairshare: no account can take work ({why})")


def cmd_pick(a) -> int:
    s = _survey()
    _nothing_to_rank(s)
    best = s.verdicts[0]
    if a.json:
        print(json.dumps({"account": best.account.id, "eligible": best.eligible,
                          "reason": best.reason, "config_dir": str(best.account.config_dir)}))
    else:
        print(best.account.id)
    return 0 if best.eligible else 75   # 75: every account is near a limit


def cmd_capture(a) -> int:
    """Runs as the provider's usage hook. Must never raise or print noise. With --then, the
    user's own command for that hook gets the same stdin and prints the line."""
    raw = sys.stdin.buffer.read()
    try:
        accounts, _ = _load()
        acct = next(x for x in accounts if x.id == a.account)
        fresh = providers.get(acct.provider).parse_capture(json.loads(raw))
        if fresh:
            now = time.time()
            store.record(acct.id, fresh, now)
            if not a.then:
                print(" · ".join(f"{k} {w.used_pct:.0f}%" for k, w in fresh.items()))
    except Exception:
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


def _launch(v: Verdict, args: list[str]) -> int:
    """Start the provider as a child, in its clean environment, and return its exit code."""
    prov = providers.get(v.account.provider)
    cmd, env = prov.launch(v.account)
    print(f"fairshare -> {v.account.id} ({v.reason})", file=sys.stderr)
    if dropped := prov.dropped_login_vars():
        _say(f"ignoring {', '.join(dropped)} from the environment; the account's own login is used")
    return session.run_child(cmd + args, env)


def _wait_until(t: float) -> None:
    while (left := t - _now()) > 0:
        _sleep(min(left, 60.0))      # short naps: a sleeping laptop must not oversleep the target


def _run_account(account_id: str, args: list[str]) -> int:
    """The user's explicit choice: no ranking, no refusal, no prompt. Only an account that is
    not signed in with a subscription is stopped."""
    accounts, tuning = _load()
    acct = next((x for x in accounts if x.id == account_id), None)
    if acct is None:
        _say(f"no account '{account_id}' (configured: {', '.join(x.id for x in accounts)})")
        return 2
    prov = providers.get(acct.provider)
    st = prov.account_status(acct)
    if st is None:
        if not prov.login_present(acct):
            _say(f"{acct.id} is not signed in (the sign-in check failed and no login was found)")
            return 1
        _say(f"sign-in check failed for {acct.id}; went by the login file and the plan in "
             "accounts.toml")
    elif not st.logged_in:
        _say(f"{acct.id} is not signed in")
        return 1
    elif not st.usable:
        _say(f"{acct.id} is not a subscription login")
        return 1
    elif st.plan and st.plan != acct.plan.name:
        acct = replace(acct, plan=load_plan(st.plan))
    now = _now()
    snaps = {acct.id: s} if (s := store.load(acct.id)) else {}
    v = rank([acct], snaps, now, tuning)[0]
    if v.eligible:
        _grace_warning(v, snaps, tuning, now)
    else:
        _say(f"warning: {acct.id} is near a limit: {v.reason}; {_usable(v, now)}")
    return _launch(v, args)


def cmd_run(a) -> int:
    if a.account:
        return _run_account(a.account, a.args)
    try:
        while True:
            s = _survey()
            if s.out:
                _say("skipped " + ", ".join(f"{x.id} ({why})" for x, why in s.out))
            if s.failed:
                _say(f"sign-in check failed for {', '.join(s.failed)}; went by the login file "
                     "and the plan in accounts.toml")
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


def _tilde(p: Path, home: Path) -> str:
    return f"~/{p.relative_to(home)}" if p.is_relative_to(home) and p != home else str(p)


def _candidates(old: dict, home: Path) -> list[tuple[dict, str]]:
    """Accounts already configured, then discovered ones not yet in the config, each with the
    plan its provider reports when it says (else the provider's default)."""
    out = [(dict(a), "accounts.toml") for a in old.get("account", [])]
    known = {Path(a["config_dir"]).expanduser() for a, _ in out}
    new = [(prov, c) for prov in providers.REGISTRY.values()
           for c in prov.discover(home) if c.config_dir not in known]
    asked = [Account(c.id, c.provider, load_plan(prov.default_plan), c.config_dir)
             for prov, c in new]
    for (prov, c), st in zip(new, _probe(asked)):
        out.append(({"id": c.id, "plan": (st and st.plan) or prov.default_plan,
                     "config_dir": _tilde(c.config_dir, home)}, c.found))
    return out


def _ask(found: list[tuple[dict, str]]) -> list[dict]:
    """Thin prompt layer: confirm or edit each id and plan; '-' drops an account."""
    print("Found:" if found else "Found no accounts (name them with --account ID=DIR:PLAN).")
    for a, where in found:
        print(f"  {a['id']:<12} {a['config_dir']:<24} ({where})")
    print(f"Plans: {', '.join(plan_names())}. Enter keeps [default]; id '-' drops the account.")
    entries = []
    for a, _ in found:
        new_id = input(f"{a['config_dir']}  id [{a['id']}]: ").strip() or a["id"]
        if new_id == "-":
            continue
        plan = input(f"{a['config_dir']}  plan [{a['plan']}]: ").strip() or a["plan"]
        entries.append({**a, "id": new_id, "plan": plan})
    return entries


def _account_flag(s: str) -> dict:
    ident, eq, rest = s.partition("=")
    d, colon, plan = rest.rpartition(":")
    if not (eq and colon and ident and d and plan):
        raise SystemExit(f"fairshare: --account wants id=dir:plan, got {s!r}")
    return {"id": ident, "plan": plan, "config_dir": d}


def cmd_init(a) -> int:
    """Shows what was found, asks, and writes only accounts.toml (with a backup)."""
    path, home = store.config_path(), Path.home()
    old_text = path.read_text() if path.exists() else ""
    old = tomllib.loads(old_text)
    if a.account:
        entries = [_account_flag(x) for x in a.account]
    elif a.yes:
        entries = [e for e, _ in _candidates(old, home)]
    else:
        entries = _ask(_candidates(old, home))
    if not entries:
        raise SystemExit("fairshare: no accounts; nothing written")
    try:
        new_text = render_config(entries, old.get("policy", {}))
    except ValueError as e:
        raise SystemExit(f"fairshare: {e}; nothing written")
    if new_text == old_text:
        print(f"{path} is already up to date.")
        return 0
    sys.stdout.writelines(difflib.unified_diff(
        old_text.splitlines(True), new_text.splitlines(True), str(path), f"{path} (new)"))
    if not a.yes and input(f"Write {path}? [y/N] ").strip().lower() != "y":
        print("Nothing written.")
        return 1
    backup = store.write_with_backup(path, new_text)
    print(f"Wrote {path}" + (f" (backup: {backup})" if backup else ""))
    return 0


def _yes(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


def _offer(changes: dict[Path, str], files: dict[Path, str | None], dry_run: bool) -> None:
    """Show each change as a diff; write it, after a backup, only on a yes for that file."""
    for p, new in changes.items():
        sys.stdout.writelines(difflib.unified_diff(
            (files.get(p) or "").splitlines(True), new.splitlines(True), str(p), f"{p} (new)"))
        if dry_run:
            continue
        if _yes(f"Change {p}?"):
            backup = store.write_with_backup(p, new)
            print("  changed" + (f" (backup: {backup})" if backup else ""))
        else:
            print("  left as is")


def _by_provider(accounts: list[Account]):
    """(provider, its accounts) for each provider that has any."""
    for prov in providers.REGISTRY.values():
        if mine := [x for x in accounts if x.provider == prov.name]:
            yield prov, mine


def _read(p: Path) -> str | None:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _files(accounts: list[Account]) -> dict[Path, str | None]:
    return {p: _read(p) for prov, mine in _by_provider(accounts)
            for p in (prov.capture_file(x) for x in mine)}


def _installed() -> str | None:
    """Absolute path of the installed `fairshare` command, as the shell would find it."""
    found = shutil.which("fairshare")
    return os.path.abspath(found) if found else None


def cmd_apply(a) -> int:
    """Wire each account's usage hook to the installed fairshare command."""
    accounts, _ = _load()
    if not (installed := _installed()):
        raise SystemExit("fairshare: no installed `fairshare` found on PATH; install it first "
                         "(pipx install .), then run `fairshare apply` again")
    files = _files(accounts)
    try:
        changes = {p: t for prov, mine in _by_provider(accounts)
                   for p, t in prov.wire(files, mine, shlex.quote(installed)).items()}
    except ValueError as e:
        raise SystemExit(f"fairshare: {e}; nothing changed")
    _offer(changes, files, a.dry_run)
    if not a.dry_run:
        print("`fairshare uninstall` reverses this.")
    return 0


def cmd_uninstall(a) -> int:
    """Reverse apply: unwrap each account's usage hook. accounts.toml and snapshots are kept."""
    accounts = _load()[0] if store.config_path().exists() else []
    files = _files(accounts)
    try:
        changes = {p: t for prov, mine in _by_provider(accounts)
                   for p, t in prov.unwire(files, mine).items()}
    except ValueError as e:
        raise SystemExit(f"fairshare: {e}; nothing changed")
    _offer(changes, files, a.dry_run)
    return 0


def _runs(runner: tuple[str, ...]) -> bool:
    """Whether the fairshare command `runner` starts and answers --help."""
    try:
        return subprocess.run([*runner, "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_doctor(_a) -> int:
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
    try:
        accounts = _load()[0]
    except SystemExit:
        check(False, "accounts.toml not found (run `fairshare init`)")
    except (OSError, ValueError, KeyError):
        check(False, "accounts.toml is not valid (run `fairshare init`)")
    for a, st in zip(accounts, _probe(accounts)):
        how = ("answers: " + ("signed out" if not st.logged_in else
                              "not a subscription login" if not st.usable else
                              f"signed in, plan {st.plan or a.plan.name}")) if st else "gave no answer"
        check(st is not None, f"{a.id}: sign-in check {how}")
    callers: dict[tuple[str, ...], list[str]] = {}
    for prov, mine in _by_provider(accounts):
        for a in mine:
            cap = prov.wired(_read(prov.capture_file(a)), a.id)
            check(cap is not None, f"{a.id}: usage hook "
                  + ("calls fairshare capture" if cap else "does not call fairshare capture "
                                                           "(run `fairshare apply`)"))
            if cap:
                callers.setdefault(cap.runner, []).append(a.id)
    if not callers and (installed := _installed()):
        callers[(installed,)] = []
    if not callers:
        check(False, "fairshare command not found on PATH")
    for runner, ids in callers.items():
        who = f"called by the usage hook of {', '.join(ids)}" if ids else "installed"
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
    pk = sub.add_parser("pick"); pk.add_argument("--json", action="store_true"); pk.set_defaults(f=cmd_pick)
    cp = sub.add_parser("capture"); cp.add_argument("account")
    cp.add_argument("--then", help="the user's own command for this hook, run after recording")
    cp.set_defaults(f=cmd_capture)
    sub.add_parser("run", help="start a session on the best account (also: no command at all); "
                   "`run [-a ID] [--] ARGS...` passes ARGS to the provider unchanged")
    it = sub.add_parser("init")
    it.add_argument("--yes", action="store_true", help="accept what was found; do not ask")
    it.add_argument("--account", action="append", metavar="ID=DIR:PLAN",
                    help="use exactly these accounts instead of discovery (repeatable)")
    it.set_defaults(f=cmd_init)
    for name, f in (("apply", cmd_apply), ("uninstall", cmd_uninstall)):
        x = sub.add_parser(name)
        x.add_argument("--dry-run", action="store_true", help="print the diffs, change nothing")
        x.set_defaults(f=f)
    sub.add_parser("doctor", help="check the setup, read-only").set_defaults(f=cmd_doctor)
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] == "run":
        return cmd_run(_run_args(p, argv[1:]))
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
