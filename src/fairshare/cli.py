"""fairshare CLI: status | pick | capture | run | init | shim install."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import fairshare

from . import providers, store
from .providers import claude
from .model import load_accounts
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
    """Runs as the provider's statusLine command. Must never raise or print noise."""
    try:
        accounts, _ = _load()
        acct = next(x for x in accounts if x.id == a.account)
        fresh = providers.get(acct.provider).parse_capture(json.load(sys.stdin))
        if fresh:
            now = time.time()
            store.save(acct.id, store.merge(store.load(acct.id), fresh, now))
            print(" · ".join(f"{k} {w.used_pct:.0f}%" for k, w in fresh.items()))
    except Exception:
        pass
    return 0


def cmd_run(a) -> int:
    best = _verdicts()[0]
    cmd, env = providers.get(best.account.provider).launch(best.account)
    print(f"fairshare -> {best.account.id} ({best.reason})", file=sys.stderr)
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    os.execvpe(cmd[0], cmd + args, {**os.environ, **env})


def cmd_init(_a) -> int:
    print(f"""Create {store.config_path()}:

  [[account]]
  id = "work"
  plan = "claude-pro"          # or claude-max5x, claude-max20x
  config_dir = "~/.claude-work"

Then, in EACH account's settings.json, add (one capture command per account):

  "statusLine": {{"type": "command", "command": "fairshare capture work"}}

fairshare never writes your settings and never touches your logins.""")
    return 0


def cmd_shim_install(a) -> int:
    d = Path(a.dir).expanduser().resolve()
    dest = d / "claude"
    if dest.exists() and not claude.is_shim(str(dest)):
        raise SystemExit(f"fairshare: {dest} exists and is not a fairshare shim; not touching it")
    d.mkdir(parents=True, exist_ok=True)
    body = claude.shim_script(sys.executable, str(Path(fairshare.__file__).resolve().parent.parent))
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    print(f"Wrote {dest}. Put its directory first on PATH, e.g. in your shell rc:\n\n"
          f'  export PATH="{d}:$PATH"')
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fairshare", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(f=cmd_status)
    pk = sub.add_parser("pick"); pk.add_argument("--json", action="store_true"); pk.set_defaults(f=cmd_pick)
    cp = sub.add_parser("capture"); cp.add_argument("account"); cp.set_defaults(f=cmd_capture)
    r = sub.add_parser("run"); r.add_argument("args", nargs=argparse.REMAINDER); r.set_defaults(f=cmd_run)
    sub.add_parser("init").set_defaults(f=cmd_init)
    sh = sub.add_parser("shim").add_subparsers(dest="shim_cmd", required=True)
    si = sh.add_parser("install")
    si.add_argument("--dir", default=str(Path.home() / ".local/share/fairshare/bin"))
    si.set_defaults(f=cmd_shim_install)
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
