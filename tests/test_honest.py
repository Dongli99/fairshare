"""Honest when everything is spent, and the sign-in check. A fake `claude` on a temp PATH serves
the recorded outputs in tests/fixtures/<version>/; HOME, config and state are temp dirs; the
clock and the sleep are injected. The real claude is never run; a fake `fairshare` stands in
for the installed command."""
import calendar
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, session, store
from fairshare.model import (Account, AccountStatus, Snapshot, Window, load_accounts, load_plan,
                             plan_for, with_account)
from fairshare.providers import claude

H = 3600.0
NOW = float(calendar.timegm((2027, 1, 15, 8, 0, 0)))     # a Friday, 08:00 UTC
FIXTURES = Path(__file__).parent / "fixtures"
VERSIONS = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
EMAIL, ORG = "someone@example.invalid", "11111111-1111-4111-8111-111111111111"

FAKE_CLAUDE = """#!/bin/sh
# Serves $FAKE_AUTH/<basename of CLAUDE_CONFIG_DIR>.json, exiting with the code in the .exit file
# beside it (0 without one), and records how it was called. The usage probe (last argument
# /usage) is logged on its own, to usage-<name> and usage-env-<name>, and answered from
# <name>.usage (exit code in
# <name>.usage-exit); with no such file it fails (exit 1, no output), so a test that serves no
# usage sees the snapshots it wrote, plus one "usage probe failed" line per account.
[ "$1" = "--version" ] && { echo "9.9.9 (Fake Code)"; exit 0; }
name=$(basename "$CLAUDE_CONFIG_DIR")
for last; do :; done
if [ "$last" = "/usage" ]; then
  printf '%s\\n' "$@" >> "$FAKE_LOG/usage-$name"
  env > "$FAKE_LOG/usage-env-$name"
  [ -n "$FAKE_USAGE_SLEEP" ] && exec sleep "$FAKE_USAGE_SLEEP"
  [ -f "$FAKE_AUTH/$name.usage" ] || exit 1
  cat "$FAKE_AUTH/$name.usage"; exit "$(cat "$FAKE_AUTH/$name.usage-exit" 2>/dev/null || echo 0)"
fi
printf '%s\\n' "$@" > "$FAKE_LOG/args-$name"
env > "$FAKE_LOG/env-$name"
[ -n "$FAKE_SLEEP" ] && exec sleep "$FAKE_SLEEP"
case "$1" in      # a sign-in check is answered; a session (no such command) is quiet
  auth|whoami) [ -f "$FAKE_AUTH/$name.json" ] || exit 1
    cat "$FAKE_AUTH/$name.json"; exit "$(cat "$FAKE_AUTH/$name.exit" 2>/dev/null || echo 0)" ;;
  *) exit 0 ;;
esac
"""
FAKE_FAIRSHARE = "#!/bin/sh\nexit 0\n"      # the installed command: answers --help, does nothing


def fixture(version: Path, name: str, **override) -> dict:
    return {**json.loads((version / f"{name}.json").read_text()), **override}


def serve(version: Path, name: str, auth: Path, id: str, **override):
    """Have the fake claude answer for account `id` as the recorded `name` did, exit code too."""
    (auth / f"{id}.json").write_text(json.dumps(fixture(version, name, **override)))
    code = version / f"{name}.exit"
    (auth / f"{id}.exit").write_text(code.read_text() if code.exists() else "0")


class Fleet(unittest.TestCase):
    version = VERSIONS[-1]

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.bin, self.auth, self.log = (self.tmp / n for n in ("bin", "auth", "log"))
        for d in (self.bin, self.auth, self.log, self.tmp / "home"):
            d.mkdir()
        (self.bin / "claude").write_text(FAKE_CLAUDE)
        (self.bin / "claude").chmod(0o755)
        (self.bin / "fairshare").write_text(FAKE_FAIRSHARE)
        (self.bin / "fairshare").chmod(0o755)
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.tmp / "home"), "PATH": f"{self.bin}:/usr/bin:/bin", "TZ": "UTC",
            "FAIRSHARE_CONFIG": str(self.tmp / "accounts.toml"),
            "FAIRSHARE_STATE": str(self.tmp / "state"),
            "FAKE_AUTH": str(self.auth), "FAKE_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC")) or k == "BROWSER"]:
            del os.environ[k]              # restored by env.stop; a surrounding session's own
        for k in [k for k in os.environ if k.startswith("FAIRSHARE_")
                  and k not in ("FAIRSHARE_CONFIG", "FAIRSHARE_STATE")]:
            del os.environ[k]              # a surrounding launcher's own
        time.tzset()
        self.addCleanup(time.tzset)      # runs after env.stop restores TZ
        for tool in ("claude", "fairshare"):                                  # never the real ones
            self.assertEqual(shutil.which(tool), str(self.bin / tool))
        self.clock, self.slept = [NOW], []
        for target, fn in (("_now", lambda: self.clock[0]), ("_sleep", self.sleep)):
            p = mock.patch.object(cli, target, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cli, "_fairshare_argv", lambda: [str(self.bin / "fairshare")])
        p.start()                        # a background probe starts the fake, never a real one
        self.addCleanup(p.stop)
        self.accounts = []
        (self.tmp / "hub" / "shared").mkdir(parents=True)
        cwd = os.getcwd()                # launches run in HOME, where no trust question is asked
        os.chdir(self.tmp / "home")
        self.addCleanup(os.chdir, cwd)

    def sleep(self, s):
        self.slept.append(s)
        self.clock[0] += s

    def add(self, id, plan="claude-pro", auth="pro", extra="", **override):
        """An account folder. `auth` names a fixture served for it (None: the probe fails)."""
        d = self.tmp / "hub/accounts" / id
        d.mkdir(parents=True)
        if auth:
            serve(self.version, f"auth-status-{auth}", self.auth, id, **override)
        self.accounts.append(f'[[account]]\nid = "{id}"\nplan = "{plan}"\n{extra}')
        (self.tmp / "accounts.toml").write_text(f'hub = "{self.tmp / "hub"}"\n\n'
                                                + "\n".join(self.accounts))
        return d

    def serve_usage(self, id, name="usage-week-hot.json", code=0):
        """Have the fake claude answer account `id`'s usage probe with a recorded answer."""
        (self.auth / f"{id}.usage").write_text((self.version / name).read_text())
        (self.auth / f"{id}.usage-exit").write_text(str(code))

    def snap(self, id, five=None, seven=None, at=NOW):
        w = {}
        if five:
            w["five_hour"] = Window(five[0], NOW + five[1] * H)
        if seven:
            w["seven_day"] = Window(seven[0], NOW + seven[1] * H)
        store.save(id, Snapshot(at, w))

    def confirm(self, id, subscription=True, at=NOW - 100):
        """Record that a past sign-in check called `id` a subscription login (or not)."""
        store.record_subscription(id, subscription, at)

    def go(self, *argv, tty=False, answers=(), real=False):
        """Run the CLI. A launch is recorded and not made, unless `real`: then the fake claude
        runs as the child."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch("builtins.input", side_effect=list(answers)) as asked, \
                mock.patch.object(session, "run_child",
                                  **({"wraps": session.run_child} if real
                                     else {"return_value": 0})) as launched:
            code = cli.main(list(argv))
        self.asked, self.launched = asked, launched
        return code, out.getvalue(), err.getvalue()

    def launched_dir(self):
        self.launched.assert_called_once()
        return self.launched.call_args.args[1]["CLAUDE_CONFIG_DIR"]

    def spent_pair(self):
        """a: week spent, back in 3h. b: short window hot for 5h, week mostly free. b ranks
        first (a spent week is below any usable account) but a is usable soonest."""
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", five=(10, 1), seven=(99, 3))
        self.snap("b", five=(95, 5), seven=(50, 100))


class Refusal(Fleet):
    def test_no_tty_prints_the_table_and_exits_75_without_launching(self):
        self.spent_pair()
        code, out, err = self.go("run", "--", "-p", "hi")
        self.assertEqual(code, 75)
        self.launched.assert_not_called()
        lines = {l.split()[0]: l for l in err.splitlines() if l.startswith("  ")}
        self.assertEqual(set(lines), {"a", "b"})
        self.assertIn("seven_day 99% used", lines["a"])
        self.assertIn("usable at 11:00 (in 3h00m)", lines["a"])
        self.assertIn("five_hour 95% used", lines["b"])
        self.assertIn("usable at 12:00 (in 4h00m)", lines["b"])
        self.assertEqual(out, "")

    def test_a_far_off_time_carries_the_day(self):
        self.add("a", orgId=ORG + "a")
        self.snap("a", five=(10, 1), seven=(99, 30))
        _, _, err = self.go("run")
        self.assertIn("usable at Sat 14:00 (in 30h00m)", err)

    def test_status_shows_when_each_account_is_usable(self):
        self.spent_pair()
        code, out, _ = self.go("status")
        self.assertEqual(code, 0)
        self.assertIn("usable at 12:00", [l for l in out.splitlines() if " b " in l][0])
        self.assertIn("usable at 11:00", [l for l in out.splitlines() if " a " in l][0])

    def test_eligible_top_launches_with_no_prompt_and_no_warning(self):
        self.add("a")
        self.snap("a", five=(10, 1), seven=(40, 50))
        code, _, err = self.go("run", "--", "-p", "hi", tty=True)
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))
        self.assertEqual(self.launched.call_args.args[0][1:], ["-p", "hi"])
        self.assertNotIn("warning", err)
        self.asked.assert_not_called()

    def test_hot_short_window_resetting_within_grace_warns_first(self):
        self.add("a")
        self.snap("a", five=(95, 0.5), seven=(40, 50))
        _, _, err = self.go("run")
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))
        self.assertIn("warning: a five_hour window 95% used, resets in 30m", err)

    def test_an_eligible_account_launches_though_a_hot_one_outscores_it(self):
        # a: half its week left, resetting in 2h (score 25); its 5h window 90% for 3h: hot,
        # scaled to 2.5 and still ranked first, but it takes no session. b can work.
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", five=(90, 3), seven=(50, 2))
        self.snap("b", seven=(50, 96))
        _, _, err = self.go("run")
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/b"))
        self.assertNotIn("no account is eligible", err)
        _, out, _ = self.go("status")
        rows = [(l[0], l[2:].split()[0]) for l in out.splitlines() if not l.startswith("   ")]
        self.assertEqual(rows, [(" ", "a"), (">", "b")])          # ranked by score, b marked


class Prompt(Fleet):
    def test_start_launches_the_top_ranked_not_the_soonest(self):
        self.spent_pair()
        code, _, err = self.go("run", tty=True, answers=["s"])
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/b"))
        self.assertIn("[w]ait for a (usable at 11:00), [s]tart on b now, [q]uit", err)
        self.assertIn("starting on b, which is near a limit", err)

    def test_quit_is_the_default_and_never_launches(self):
        self.spent_pair()
        for answer in ("", "q", "x"):
            with self.subTest(answer=answer):
                self.assertEqual(self.go("run", tty=True, answers=[answer])[0], 75)
                self.launched.assert_not_called()
        self.assertEqual(self.go("run", tty=True, answers=[EOFError()])[0], 75)   # EOF is a quit

    def test_wait_sleeps_to_the_soonest_then_launches_after_a_fresh_ranking(self):
        self.spent_pair()
        _, _, err = self.go("run", tty=True, answers=["w"])
        self.assertGreaterEqual(self.clock[0], NOW + 3 * H)
        self.assertLess(self.clock[0], NOW + 3 * H + 120)
        self.assertEqual(err.count("waiting for a, usable at 11:00"), 1)   # told once, no countdown
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))

    def test_waking_re_ranks_from_fresh_snapshots_and_never_launches_blindly(self):
        self.spent_pair()
        real = self.sleep

        def sleep_then_a_session_spends_account_a(s):
            real(s)
            self.snap("a", five=(99, 4), seven=(99, 30), at=self.clock[0])
        with mock.patch.object(cli, "_sleep", sleep_then_a_session_spends_account_a):
            code, _, err = self.go("run", tty=True, answers=["w", "q"])
        self.assertEqual(code, 75)                       # asked again: nothing was eligible
        self.launched.assert_not_called()
        self.assertEqual(self.asked.call_count, 2)
        self.assertIn("Sat 14:00", err.split("waiting for")[1])   # the second table is new

    def test_ctrl_c_while_waiting_exits_130_without_launching(self):
        self.spent_pair()
        with mock.patch.object(cli, "_sleep", side_effect=KeyboardInterrupt):
            code, _, _ = self.go("run", tty=True, answers=["w"])
        self.assertEqual(code, 130)
        self.launched.assert_not_called()


class Excluded(Fleet):
    def setUp(self):
        super().setUp()
        self.add("ok", orgId=ORG + "ok")
        self.add("out", auth="logged-out")
        self.add("key", authMethod="api-key", orgId=ORG + "key")      # logged in, not a subscription

    def test_logged_out_and_non_subscription_accounts_are_not_ranked(self):
        _, out, _ = self.go("status")
        lines = {l.split()[1] if l.startswith(">") else l.split()[0]: l for l in out.splitlines()
                 if l.strip() and not l.startswith("    ")}
        self.assertTrue(lines["ok"].startswith(">"))
        self.assertIn("left out: not signed in", lines["out"])
        self.assertIn("left out: not a subscription login", lines["key"])

    def test_run_prints_one_line_about_what_it_skipped(self):
        _, _, err = self.go("run")
        skipped = [l for l in err.splitlines() if "skipped" in l]
        self.assertEqual(len(skipped), 1)
        self.assertIn("out (not signed in)", skipped[0])
        self.assertIn("key (not a subscription login)", skipped[0])
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/ok"))

    def test_nothing_rankable_is_an_error_not_a_launch(self):
        (self.tmp / "accounts.toml").write_text(f'hub = "{self.tmp / "hub"}"\n' + self.accounts[1])
        with self.assertRaises(SystemExit) as e, \
                mock.patch.object(session, "run_child") as launched, \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main(["run"])
        self.assertIn("no account can take work", str(e.exception))
        launched.assert_not_called()


class SameLogin(Fleet):
    def setUp(self):
        super().setUp()
        self.add("one")                       # the same org and email as "two"
        self.add("two")
        self.add("other", orgId=ORG + "x")
        self.snap("one", five=(10, 1), seven=(60, 50), at=NOW - 100)
        self.snap("two", five=(10, 1), seven=(30, 50), at=NOW - 10)    # the newer snapshot
        self.snap("other", five=(10, 1), seven=(90, 50))

    def test_one_quota_is_ranked_once_by_its_newest_snapshot(self):
        s = cli._survey()
        self.assertEqual([v.account.id for v in s.verdicts], ["two", "other"])
        self.assertIn("seven_day 30% used", s.verdicts[0].reason)
        self.assertEqual([(a.id, rep) for a, rep in s.twins], [("one", "two")])

    def test_status_shows_the_other_folder_as_the_same_login(self):
        _, out, _ = self.go("status")
        self.assertIn("one", [l for l in out.splitlines() if "same login as two" in l][0])
        self.assertEqual(len([l for l in out.splitlines() if "score" in l and not l.startswith("why ")]), 2)

    def test_nothing_identifying_is_printed(self):
        for cmd in ("status", "run"):
            _, out, err = self.go(cmd)
            for secret in (EMAIL, ORG, "Example Org", "person@example"):
                self.assertNotIn(secret, out + err)


class DetectedPlan(Fleet):
    def test_a_detected_plan_beats_the_config_and_the_mismatch_is_shown(self):
        self.add("a", plan="claude-max")
        s = cli._survey()
        self.assertEqual(s.verdicts[0].account.plan.name, "claude-pro")
        _, out, _ = self.go("status")
        self.assertIn("claude-pro", out.split("\n")[0])
        self.assertIn("plan claude-max in config, detected claude-pro", out)

    def test_a_matching_plan_says_nothing(self):
        self.add("a")
        self.assertNotIn("detected", self.go("status")[1])

    def test_an_unmapped_plan_leaves_the_config_alone(self):
        self.add("a", plan="claude-max", planTier="max_5x")
        self.assertEqual(cli._survey().verdicts[0].account.plan.name, "claude-max")
        self.assertNotIn("detected", self.go("status")[1])

    def test_plan_names_are_data_in_the_plan_files(self):
        self.assertEqual(plan_for("claude", "pro"), "claude-pro")
        self.assertEqual(plan_for("claude", "max"), "claude-max")
        self.assertEqual(plan_for("claude", "ultra"), "claude-ultra")
        self.assertIsNone(plan_for("claude", "max_5x"))
        self.assertIsNone(plan_for("other-provider", "pro"))
        self.assertIsNone(plan_for("claude", None))


class ProbeFails(Fleet):
    def test_falls_back_to_the_last_confirmation_and_the_configured_plan(self):
        self.add("a", plan="claude-max", auth=None)           # claude exits non-zero
        self.add("b", auth=None)
        self.add("c", orgId=ORG + "c")
        self.confirm("a")
        self.confirm("b", False)      # confirmed once as not a subscription login
        s = cli._survey()
        self.assertEqual({v.account.id: v.account.plan.name for v in s.verdicts},
                         {"a": "claude-max", "c": "claude-pro"})
        self.assertEqual([(a.id, why) for a, why in s.out], [
            ("b", "sign-in check failed and b was never confirmed as a subscription login")])
        self.assertEqual(s.failed, ["a", "b"])

    def test_status_says_what_failed_and_what_was_used(self):
        self.add("a", auth=None)
        self.add("b", auth=None)
        self.confirm("a")
        out = self.go("status")[1]
        self.assertIn("sign-in check failed; last confirmed as a subscription login on "
                      "2027-01-15; plan claude-pro from config", out)
        self.assertNotIn("login file", out)

    def test_run_prints_one_line_and_still_launches(self):
        self.add("a", auth=None)
        self.add("b", auth=None)
        self.confirm("a")
        self.confirm("b")
        _, _, err = self.go("run")
        self.assertEqual(len([l for l in err.splitlines() if "sign-in check failed" in l]), 1)
        self.assertIn("a, b", err)
        self.launched.assert_called_once()

    def test_same_login_is_not_grouped_when_the_check_failed(self):
        self.add("a", auth=None)
        self.add("b", auth=None)
        self.confirm("a")
        self.confirm("b")
        self.assertEqual(len(cli._survey().verdicts), 2)

    def test_timeout_bad_json_and_missing_binary_all_mean_none(self):
        d = self.add("a")
        acct = Account("a", "claude", load_plan("claude-pro"), d)
        with mock.patch.object(claude, "PROBE_TIMEOUT", 0.3), \
                mock.patch.dict(os.environ, {"FAKE_SLEEP": "5"}):
            t = time.monotonic()
            self.assertIsNone(claude.account_status(acct))
            self.assertLess(time.monotonic() - t, 3)
        (self.auth / "a.json").write_text("not json")
        self.assertIsNone(claude.account_status(acct))
        (self.auth / "a.json").write_text("[1, 2]")
        self.assertIsNone(claude.account_status(acct))
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIsNone(claude.account_status(acct))


class Probe(Fleet):
    SESSION_STATE = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_JOB_DIR",
                     "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                     "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
                     "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                     "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_SESSION_ID")
    LOGIN_OVERRIDES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                       "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                       "CLAUDE_CODE_USE_FOUNDRY")

    def test_the_scrub_is_exactly_these_names(self):
        """The lists are the brief's, spelled out here on purpose: a change to one is a change
        to what reaches every child."""
        self.assertEqual(set(claude.SESSION_ENV), set(self.SESSION_STATE))
        self.assertEqual(set(claude.LOGIN_OVERRIDE_ENV), set(self.LOGIN_OVERRIDES))

    def test_the_environment_is_scrubbed_and_points_at_the_account(self):
        d = self.add("a")
        mine = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000", "CLAUDE_CODE_SOMETHING_NEW": "x",
                "CLAUDE_CONFIG_DIR": "/somewhere/else", "KEEP_ME": "yes"}
        hostile = {k: "v" for k in self.SESSION_STATE + self.LOGIN_OVERRIDES}
        with mock.patch.dict(os.environ, {**hostile, **mine}):
            status = claude.account_status(Account("a", "claude", load_plan("claude-pro"), d))
        self.assertTrue(status.logged_in)
        seen = dict(l.split("=", 1) for l in (self.log / "env-a").read_text().splitlines() if "=" in l)
        for k in hostile:
            self.assertFalse(k in seen, f"{k} reached the child")
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], str(d))
        for k in ("KEEP_ME", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_SOMETHING_NEW"):
            self.assertEqual(seen[k], mine[k], k)       # everything else passes through

    def test_clean_env_is_the_one_helper_for_children(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_X": "1", "ANTHROPIC_API_KEY": "k"}):
            env = claude.clean_env(Path("/acct"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/acct")
        self.assertEqual(env["CLAUDE_CODE_X"], "1")
        self.assertFalse("ANTHROPIC_API_KEY" in env)
        self.assertEqual(env["HOME"], os.environ["HOME"])

    def test_default_command_and_the_per_account_override(self):
        self.add("a")
        self.add("b", extra='status_args = "whoami --text"\n')
        accounts, *_ = load_accounts(self.tmp / "accounts.toml")
        self.assertEqual([a.status_args for a in accounts], [None, ("whoami", "--text")])
        for a in accounts:
            claude.account_status(a)
        self.assertEqual((self.log / "args-a").read_text().split(), ["auth", "status", "--json"])
        self.assertEqual((self.log / "args-b").read_text().split(), ["whoami", "--text"])

    def test_the_override_survives_join_adding_an_account(self):
        p = self.tmp / "round.toml"
        p.write_text('[[account]]\nid = "b"\nplan = "claude-pro"\nstatus_args = "whoami --text"\n')
        p.write_text(with_account(p.read_text(), {"id": "c", "plan": "claude-pro"}))
        self.assertEqual([a.status_args for a in load_accounts(p)[0]], [("whoami", "--text"), None])

    def test_every_account_is_asked_once_per_command(self):
        for i in "abc":
            self.add(i, orgId=ORG + i)
        with mock.patch.object(claude, "account_status", wraps=claude.account_status) as spy:
            self.go("status")
        self.assertEqual(spy.call_count, 3)


class Fixtures(Fleet):
    """The parser against every recorded Claude Code version. A new version = a new folder."""

    def test_there_is_a_fixture_folder(self):
        self.assertTrue(VERSIONS)

    def test_logged_in_subscription(self):
        for v in VERSIONS:
            with self.subTest(version=v.name):
                st = claude.parse_account_status(fixture(v, "auth-status-pro"))
                self.assertEqual((st.logged_in, st.usable, st.plan), (True, True, "claude-pro"))
                self.assertTrue(st.identity)
                self.assertNotIn("example", st.identity)      # opaque: no email or org inside

    def test_logged_out(self):
        for v in VERSIONS:
            with self.subTest(version=v.name):
                st = claude.parse_account_status(fixture(v, "auth-status-logged-out"))
                self.assertEqual(st, AccountStatus(False, False, None, None))

    def test_identity_follows_org_and_email(self):
        for v in VERSIONS:
            with self.subTest(version=v.name):
                p = lambda **o: claude.parse_account_status(fixture(v, "auth-status-pro", **o)).identity
                self.assertEqual(p(), p())
                self.assertNotEqual(p(), p(orgId="22222222-2222-4222-8222-222222222222"))
                self.assertNotEqual(p(), p(email="else@example.invalid"))
                self.assertIsNone(p(orgId=None))

    def test_the_fake_claude_serves_each_fixture_through_account_status(self):
        d = self.add("a", auth=None)
        acct = Account("a", "claude", load_plan("claude-pro"), d)
        for v in VERSIONS:
            for name, expect in (("auth-status-pro", True), ("auth-status-logged-out", False)):
                with self.subTest(version=v.name, fixture=name):
                    serve(v, name, self.auth, "a")
                    self.assertEqual(claude.account_status(acct).logged_in, expect)

    def test_an_unknown_shape_is_a_failed_probe(self):
        for raw in ({}, {"loggedIn": "yes"}, [], None, "x"):
            self.assertIsNone(claude.parse_account_status(raw))


if __name__ == "__main__":
    unittest.main()
