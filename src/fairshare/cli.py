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
from pathlib import Path

import fairshare

from . import providers, store
from .providers import claude
from .model import load_accounts, plan_names, render_config
from .policy import rank


def _load():
    path = store.config_path()
    if not path.exists():
        raise SystemExit(f"fairshare: no config at {path} (see `fairshare init`)")
    return load_accounts(path)


def _verdicts():
    accounts, tuning = _load()
    snaps = {a.id: s for a in accounts if (s := store.load(a.id))}
    return rank(accounts, snaps, time.time(), tuning)


def _in(h: float | None) -> str:
    return "-" if h is None else f"{int(h)}h{int(h % 1 * 60):02d}m"


def cmd_status(_a) -> int:
    for i, v in enumerate(_verdicts()):
        mark = ">" if i == 0 else " "
        flag = "" if v.eligible else "  [avoid]"
        print(f"{mark} {v.account.id:<12} {v.account.plan.name:<14} score {v.score:6.2f}  "
              f"{v.reason}{flag}")
    return 0


def cmd_pick(a) -> int:
    best = _verdicts()[0]
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


def cmd_run(a) -> int:
    best = _verdicts()[0]
    cmd, env = providers.get(best.account.provider).launch(best.account)
    print(f"fairshare -> {best.account.id} ({best.reason})", file=sys.stderr)
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    os.execvpe(cmd[0], cmd + args, {**os.environ, **env})


def _tilde(p: Path, home: Path) -> str:
    return f"~/{p.relative_to(home)}" if p.is_relative_to(home) and p != home else str(p)


def _candidates(old: dict, home: Path) -> list[tuple[dict, str]]:
    """Accounts already configured, then discovered ones not yet in the config."""
    out = [(dict(a), "accounts.toml") for a in old.get("account", [])]
    known = {Path(a["config_dir"]).expanduser() for a, _ in out}
    for prov in providers.REGISTRY.values():
        for c in prov.discover(home):
            if c.config_dir not in known:
                out.append(({"id": c.id, "plan": prov.default_plan,
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
