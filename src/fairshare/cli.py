"""fairshare CLI: status | pick | capture | run | init | shim install | apply | uninstall."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import fairshare

from . import providers, store
from .providers import claude
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
            signed_in = (a.config_dir / claude.LOGIN_FILE).exists()
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
    """Runs as the provider's statusLine command. Must never raise or print noise. With
    --then, the user's own statusLine command gets the same stdin and prints the line."""
    raw = sys.stdin.buffer.read()
    try:
        accounts, _ = _load()
        acct = next(x for x in accounts if x.id == a.account)
        fresh = providers.get(acct.provider).parse_capture(json.loads(raw))
        if fresh:
            now = time.time()
            store.save(acct.id, store.merge(store.load(acct.id), fresh, now))
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


def _grace_warning(s: Survey, v: Verdict) -> None:
    """An account that is eligible only because its hot short window resets soon."""
    w = (snap := s.snaps.get(v.account.id)) and snap.windows.get(v.account.plan.short_name)
    if w and w.used_pct >= s.tuning.short_hot_pct and 0 < w.resets_at - s.now:
        left = (w.resets_at - s.now) / HOUR
        if left < s.tuning.grace_hours:
            _say(f"warning: {v.account.id} {v.account.plan.short_name} window "
                 f"{w.used_pct:.0f}% used, resets in {int(left * 60)}m")


def _launch(v: Verdict, args: list[str]):
    cmd, env = providers.get(v.account.provider).launch(v.account)
    print(f"fairshare -> {v.account.id} ({v.reason})", file=sys.stderr)
    os.execvpe(cmd[0], cmd + args, {**os.environ, **env})


def _wait_until(t: float) -> None:
    while (left := t - _now()) > 0:
        _sleep(min(left, 60.0))      # short naps: a sleeping laptop must not oversleep the target


def cmd_run(a) -> int:
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
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
                _grace_warning(s, top)
                return _launch(top, args)
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
                return _launch(top, args)
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


def _self_command() -> str:
    """A shell command that runs this very fairshare, however it was installed. -I keeps the
    current directory and PYTHON* variables out of it."""
    src = json.dumps(str(Path(fairshare.__file__).resolve().parent.parent))
    code = f"import sys; sys.path.insert(0, {src}); from fairshare.cli import main; sys.exit(main())"
    return f"{shlex.quote(sys.executable)} -I -c {shlex.quote(code)}"


def _shim_dir(a) -> Path:
    return Path(a.dir).expanduser().resolve()


def _write_shim(d: Path) -> Path:
    dest = d / "claude"
    if dest.exists() and not claude.is_shim(str(dest)):
        raise SystemExit(f"fairshare: {dest} exists and is not a fairshare shim; not touching it")
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        f.write(claude.shim_script(_self_command()))
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return dest


def cmd_shim_install(a) -> int:
    d = _shim_dir(a)
    print(f"Wrote {_write_shim(d)}. Put its directory first on PATH, e.g. in your shell rc:\n\n"
          f'  export PATH="{d}:$PATH"')
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


def _files(accounts, home: Path) -> dict[Path, str | None]:
    paths = [x.config_dir / "settings.json" for x in accounts] + [home / r for r in claude.RC_FILES]
    return {p: p.read_text() if p.is_file() else None for p in paths}


def cmd_apply(a) -> int:
    """Install the shim, wire each account's statusLine, put the shim first on PATH."""
    accounts, _ = _load()
    home, d = Path.home(), _shim_dir(a)
    files = _files(accounts, home)
    try:
        changes = claude.wire(files, accounts, home, os.environ.get("SHELL", ""),
                              _self_command(), d)
    except ValueError as e:
        raise SystemExit(f"fairshare: {e}; nothing changed")
    print(f"The shim: {d / 'claude'} (fairshare's own file; routes plain `claude`).")
    if not a.dry_run and _yes(f"Write {d / 'claude'}?"):
        _write_shim(d)
    _offer(changes, files, a.dry_run)
    if not a.dry_run:
        print("Open a new shell for the PATH change. `fairshare uninstall` reverses all of this.")
    return 0


def cmd_uninstall(a) -> int:
    """Reverse apply: unwrap statusLines, drop the rc block, restore the alias, remove the shim.
    accounts.toml and snapshots are kept."""
    accounts = _load()[0] if store.config_path().exists() else []
    home, shim = Path.home(), _shim_dir(a) / "claude"
    files = _files(accounts, home)
    try:
        changes = claude.unwire(files, accounts, home)
    except ValueError as e:
        raise SystemExit(f"fairshare: {e}; nothing changed")
    if claude.is_shim(str(shim)):
        print(f"Remove the shim {shim}.")
        if not a.dry_run and _yes(f"Remove {shim}?"):
            shim.unlink()
    _offer(changes, files, a.dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fairshare", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(f=cmd_status)
    pk = sub.add_parser("pick"); pk.add_argument("--json", action="store_true"); pk.set_defaults(f=cmd_pick)
    cp = sub.add_parser("capture"); cp.add_argument("account")
    cp.add_argument("--then", help="the user's own statusLine command, run after recording")
    cp.set_defaults(f=cmd_capture)
    r = sub.add_parser("run"); r.add_argument("args", nargs=argparse.REMAINDER); r.set_defaults(f=cmd_run)
    it = sub.add_parser("init")
    it.add_argument("--yes", action="store_true", help="accept what was found; do not ask")
    it.add_argument("--account", action="append", metavar="ID=DIR:PLAN",
                    help="use exactly these accounts instead of discovery (repeatable)")
    it.set_defaults(f=cmd_init)
    sh = sub.add_parser("shim").add_subparsers(dest="shim_cmd", required=True)
    si = sh.add_parser("install")
    shim_dir = str(Path.home() / ".local/share/fairshare/bin")
    si.add_argument("--dir", default=shim_dir)
    si.set_defaults(f=cmd_shim_install)
    for name, f in (("apply", cmd_apply), ("uninstall", cmd_uninstall)):
        x = sub.add_parser(name)
        x.add_argument("--dry-run", action="store_true", help="print the diffs, change nothing")
        x.add_argument("--dir", default=shim_dir, help="the shim's directory")
        x.set_defaults(f=f)
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
