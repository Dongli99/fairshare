"""Stage 5b, part A: `fairshare trust` (copy, never create) and `fairshare worker`. Same fleet as
test_honest (temp HOME, config, state and hub; fake claude and fairshare first on PATH, proved
in Fleet.setUp; inherited CLAUDE*, FAIRSHARE_* and BROWSER cleared). The fake claude is wrapped
so that `--bg --name X` makes X a live session and `agents --json` lists the live ones."""
import json
import os
import time
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import ROLE_WORKER, Worker, load_accounts
from fairshare.providers import claude

from test_honest import ORG, Fleet
from test_hub import write

TRUST = claude.STATE_TRUST
PARENT = "9f8e7d6c-0000-4000-8000-00000000000a"

WRAPPER = """#!/bin/sh
# Records the folder it ran in; `--bg --name X ...` makes X live (unless $FAKE_NO_JOIN), and
# `agents --json` lists the live sessions as sessions s-X (nothing at all with $FAKE_NO_LIST).
name=$(basename "$CLAUDE_CONFIG_DIR")
if [ "$1" = "agents" ]; then
  [ -n "$FAKE_NO_LIST" ] && exit 0
  printf '['; sep=
  [ -f "$FAKE_LOG/live" ] && while read n; do
    printf '%s{"sessionId": "s-%s", "name": "%s", "status": "busy", "pid": 1}' "$sep" "$n" "$n"
    sep=,
  done < "$FAKE_LOG/live"
  printf ']\\n'; exit 0
fi
if [ "$1" = "--bg" ]; then
  pwd -P > "$FAKE_LOG/cwd-$name"
  [ -n "$FAKE_BG_FAIL" ] && { echo "bg failed" >&2; exit 3; }
  [ -z "$FAKE_NO_JOIN" ] && echo "$3" >> "$FAKE_LOG/live"
fi
exec "{real}" "$@"
"""


class Work(Fleet):
    """Accounts a (ranks first), b and c; a project folder by its canonical path."""

    def setUp(self):
        super().setUp()
        for i, week in zip("abc", (10, 40, 50)):
            self.add(i, orgId=ORG + i)
            self.snap(i, five=(10, 1), seven=(week, 50))
        self.here = self.tmp / "work" / "project"
        self.here.mkdir(parents=True)
        os.chdir(self.here)
        self.here = Path(os.getcwd())
        real = self.bin / "claude-real"
        os.rename(self.bin / "claude", real)
        write(self.bin / "claude", WRAPPER.replace("{real}", str(real))).chmod(0o755)

    def state(self, id) -> Path:
        return self.tmp / "hub/accounts" / id / claude.STATE_FILE

    def trust_of(self, id, path=None):
        if not self.state(id).exists():
            return None
        e = json.loads(self.state(id).read_text()).get("projects", {}).get(str(path or self.here))
        return e and e.get(TRUST)

    def trusted(self, *ids, path=None):
        for i in ids:
            self.state(i).write_text(json.dumps({"projects": {str(path or self.here):
                                                              {TRUST: True}}, "keep": i}))


class TrustCommand(Work):
    def test_copies_to_every_account_lacking_it_one_line_each(self):
        self.trusted("a")
        code, out, err = self.go("trust")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"b now trusts {self.here} (copied from a)\n"
                              f"c now trusts {self.here} (copied from a)\n")
        self.assertEqual([self.trust_of(i) for i in "abc"], [True, True, True])
        self.assertEqual(json.loads(self.state("a").read_text())["keep"], "a")   # source untouched

    def test_keeps_the_other_keys_and_backs_up(self):
        self.trusted("a")
        self.state("b").write_text(json.dumps({"userID": "u-b", "projects": {"/x": {"k": 1}}}))
        self.go("trust")
        d = json.loads(self.state("b").read_text())
        self.assertEqual((d["userID"], d["projects"]["/x"]), ("u-b", {"k": 1}))
        self.assertTrue(list((self.tmp / "hub/backups").glob("*-trust-b/" + claude.STATE_FILE)))

    def test_a_path_argument(self):
        other = self.tmp / "work" / "other"
        other.mkdir()
        self.trusted("b", path=other.resolve())
        code, out, _ = self.go("trust", str(other))
        self.assertEqual(code, 0)
        self.assertEqual([self.trust_of(i, other.resolve()) for i in "ac"], [True, True])
        self.assertEqual(self.go("trust", str(self.tmp / "nowhere"))[0], 2)

    def test_nothing_needed(self):
        self.trusted("a", "b", "c")
        self.assertEqual(self.go("trust"), (0, f"every account already trusts {self.here}; "
                                               "nothing to copy\n", ""))

    def test_refused_in_home(self):
        self.trusted("a", path=(self.tmp / "home").resolve())
        os.chdir(self.tmp / "home")
        code, out, err = self.go("trust")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is HOME: fairshare writes no trust in HOME", err)
        self.assertEqual([self.state(i).exists() for i in "bc"], [False, False])

    def test_refused_when_no_account_trusts_it(self):
        code, out, err = self.go("trust")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("fairshare never creates trust: start a session there from a terminal "
                      "(`fairshare`) and answer the question", err)
        self.assertEqual([self.state(i).exists() for i in "abc"], [False] * 3)

    def test_refused_for_a_remembered_only(self):
        self.trusted("a")
        store.remember_trust_only(self.here)
        code, out, err = self.go("trust")
        self.assertEqual((code, out), (1, ""))
        self.assertIn('you answered "only"', err)
        self.assertIn("nothing copied", err)
        self.assertEqual([self.state(i).exists() for i in "bc"], [False, False])

    def test_a_held_lock_times_out_for_that_account_only(self):
        self.trusted("a")
        (self.tmp / "hub/accounts/b" / claude.STATE_LOCK).mkdir()
        with mock.patch.object(claude, "STATE_LOCK_WAIT", 0.3):
            code, out, err = self.go("trust")
        self.assertEqual(code, 1)
        self.assertIn("b: trust not written", err)
        self.assertIn("held by someone else", err)
        self.assertEqual(out, f"c now trusts {self.here} (copied from a)\n")
        self.assertFalse(self.state("b").exists())


class WorkerCommand(Work):
    def worker(self, *argv, env=None, inside=True):
        e = {**({"CLAUDE_CODE_SESSION_ID": PARENT} if inside else {}), **(env or {})}
        with mock.patch.dict(os.environ, e):
            return self.go("worker", *argv)

    def bg_args(self, account):
        return (self.log / f"args-{account}").read_text().splitlines()

    def test_outside_a_session_exits_2(self):
        code, _, err = self.worker("do it", inside=False)
        self.assertEqual(code, 2)
        self.assertIn("not inside a session", err)
        self.assertFalse((self.log / "live").exists())

    def test_starts_on_the_top_account_named_prefixed_joined_and_recorded(self):
        self.trusted("a")
        code, out, err = self.worker("Refactor the parser.")
        self.assertEqual(code, 0, err)
        name = "w-9f8e7d6c-1"
        self.assertEqual(out, f"worker {name} started on a (session s-w-9f8e)\n")
        args = self.bg_args("a")
        self.assertEqual(args[:3], ["--bg", "--name", name])
        self.assertEqual(args[3:], [cli.WORKER_LINE.format(parent=PARENT), "",
                                    "Refactor the parser."])
        self.assertIn(f'You are a worker for session {PARENT}. When fairshare asks for a '
                      'checkpoint, run fairshare checkpoint "<what is done, what is next>".', args)
        self.assertEqual(Path((self.log / "cwd-a").read_text().strip()), self.here)
        w = store.worker(name)
        self.assertEqual((w.parent, w.account, w.cwd, w.started, w.session),
                         (PARENT, "a", str(self.here), self.clock[0], f"s-{name}"))
        rec = store.session(f"s-{name}")
        self.assertEqual((rec.role, rec.parent, rec.account), (ROLE_WORKER, PARENT, "a"))

    def test_the_environment_is_the_accounts_own_with_no_browser(self):
        self.trusted("a")
        self.worker("x", env={"BROWSER": "/usr/bin/true", "FAIRSHARE_LAUNCHER": "1",
                              "CLAUDE_PID": "5"})
        env = dict(l.split("=", 1) for l in (self.log / "env-a").read_text().splitlines()
                   if "=" in l)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.tmp / "hub/accounts/a"))
        for gone in ("BROWSER", "FAIRSHARE_LAUNCHER", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
            self.assertNotIn(gone, env)

    def test_names_are_unique_among_the_parents_workers(self):
        self.trusted("a")
        self.worker("one")
        self.worker("two")
        self.assertEqual([w.name for w in store.workers(PARENT)],
                         ["w-9f8e7d6c-1", "w-9f8e7d6c-2"])
        code, _, _ = self.worker("--name", "mine", "three")
        self.assertEqual((code, store.worker("mine").parent), (0, PARENT))
        code, _, err = self.worker("--name", "mine", "four")
        self.assertEqual(code, 2)
        self.assertIn("a worker named mine is recorded already", err)
        self.assertEqual(self.worker("--name", "../x", "five")[0], 2)

    def test_account_choice(self):
        self.trusted("a", "b", "c")
        self.snap("a", five=(100, 3), seven=(10, 50))           # a is out: b is the top eligible
        self.assertEqual(self.worker("x")[0], 0)
        self.assertEqual(store.workers()[0].account, "b")
        self.assertEqual(self.worker("--account", "c", "y")[0], 0)
        self.assertEqual(store.workers()[1].account, "c")
        code, _, err = self.worker("--account", "zz", "z")
        self.assertEqual(code, 2)
        self.assertIn("no account 'zz'", err)

    def test_trust_is_copied_on_the_way(self):
        self.trusted("c")                                       # a is the top account; c trusts
        code, out, err = self.worker("x")
        self.assertEqual(code, 0, err)
        self.assertIn(f"a now trusts {self.here} (copied from c)", err)
        self.assertTrue(self.trust_of("a"))
        self.assertIsNone(self.trust_of("b"))                   # only the account starting
        self.assertEqual(self.bg_args("a")[0], "--bg")

    def test_refused_when_trust_cannot_be_had(self):
        code, _, err = self.worker("x")
        self.assertEqual(code, 1)
        self.assertIn("no account trusts it, so there is nothing to copy", err)
        self.trusted("c")
        store.remember_trust_only(self.here)
        code, _, err = self.worker("x")                         # c trusts and is eligible: c
        self.assertEqual((code, store.workers()[0].account), (0, "c"))
        self.assertEqual(store.workers()[0].name, "w-9f8e7d6c-1")
        self.assertFalse((self.log / "args-a").exists() and "--bg" in self.bg_args("a"))

    def test_a_failed_start_records_nothing(self):
        self.trusted("a")
        code, out, _ = self.worker("x", env={"FAKE_BG_FAIL": "1"})
        self.assertEqual((code, out, store.workers()), (3, "", []))

    def test_a_join_that_times_out_says_so_and_a_later_retry_finds_it(self):
        self.trusted("a")
        start = self.clock[0]
        code, out, _ = self.worker("x", env={"FAKE_NO_LIST": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(out, "worker w-9f8e7d6c-1 started on a; its session id is not known "
                              "yet (fairshare finds it by name later)\n")
        self.assertGreaterEqual(self.clock[0] - start, cli.JOIN_WAIT)   # waited, on the clock
        self.assertLess(self.clock[0] - start, cli.JOIN_WAIT + 1)
        self.assertIsNone(store.worker("w-9f8e7d6c-1").session)
        accounts = load_accounts(self.tmp / "accounts.toml")[0]
        found = cli._join_pending(store.workers(PARENT), accounts, self.clock[0])
        self.assertEqual(found, {"w-9f8e7d6c-1": "s-w-9f8e7d6c-1"})
        self.assertEqual(store.session("s-w-9f8e7d6c-1").role, ROLE_WORKER)

    def test_a_retry_gives_up_on_an_old_worker(self):
        store.add_worker(Worker("old", PARENT, "a", str(self.here), self.clock[0]))
        (self.log / "live").write_text("old\n")
        accounts = load_accounts(self.tmp / "accounts.toml")[0]
        self.assertEqual(cli._join_pending(store.workers(), accounts,
                                           self.clock[0] + cli.JOIN_RETRY_S), {})
        self.assertEqual(cli._join_pending(store.workers(), accounts, self.clock[0] + 60),
                         {"old": "s-old"})


class ListSessions(Work):
    def test_items_and_shapes(self):
        acct = load_accounts(self.tmp / "accounts.toml")[0][0]
        (self.log / "live").write_text("x\n")
        self.assertEqual([(s.id, s.name, s.busy) for s in claude.list_sessions(acct)],
                         [("s-x", "x", True)])
        with mock.patch.dict(os.environ, {"FAKE_NO_LIST": "1"}):
            self.assertIsNone(claude.list_sessions(acct))
