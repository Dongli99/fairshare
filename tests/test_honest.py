"""Honest when everything is spent, and the sign-in check. A fake `claude` on a temp PATH serves
the recorded outputs in tests/fixtures/<version>/; HOME, config and state are temp dirs; the
clock and the sleep are injected. The real claude is never run."""
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

from fairshare import cli, store
from fairshare.model import (Account, AccountStatus, Snapshot, Window, load_accounts, load_plan,
                             plan_for, render_config)
from fairshare.providers import claude

H = 3600.0
NOW = float(calendar.timegm((2027, 1, 15, 8, 0, 0)))     # a Friday, 08:00 UTC
FIXTURES = Path(__file__).parent / "fixtures"
VERSIONS = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
EMAIL, ORG = "someone@example.invalid", "11111111-1111-4111-8111-111111111111"

FAKE_CLAUDE = """#!/bin/sh
# Serves $FAKE_AUTH/<basename of CLAUDE_CONFIG_DIR>.json, and records how it was called.
name=$(basename "$CLAUDE_CONFIG_DIR")
printf '%s\\n' "$@" > "$FAKE_LOG/args-$name"
env > "$FAKE_LOG/env-$name"
[ -n "$FAKE_SLEEP" ] && exec sleep "$FAKE_SLEEP"
[ -f "$FAKE_AUTH/$name.json" ] && exec cat "$FAKE_AUTH/$name.json"
exit 1
"""


def fixture(version: Path, name: str, **override) -> dict:
    return {**json.loads((version / f"{name}.json").read_text()), **override}


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
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.tmp / "home"), "PATH": f"{self.bin}:/usr/bin:/bin", "TZ": "UTC",
            "FAIRSHARE_CONFIG": str(self.tmp / "accounts.toml"),
            "FAIRSHARE_STATE": str(self.tmp / "state"),
            "FAKE_AUTH": str(self.auth), "FAKE_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        time.tzset()
        self.addCleanup(time.tzset)      # runs after env.stop restores TZ
        self.assertEqual(shutil.which("claude"), str(self.bin / "claude"))   # never the real one
        self.clock, self.slept = [NOW], []
        for target, fn in (("_now", lambda: self.clock[0]), ("_sleep", self.sleep)):
            p = mock.patch.object(cli, target, fn)
            p.start()
            self.addCleanup(p.stop)
        self.accounts = []

    def sleep(self, s):
        self.slept.append(s)
        self.clock[0] += s

    def add(self, id, plan="claude-pro", auth="pro", login=True, extra="", **override):
        """An account folder. `auth` names a fixture served for it (None: the probe fails)."""
        d = self.tmp / "accts" / id
        d.mkdir(parents=True)
        if login:
            (d / claude.LOGIN_FILE).write_text("")
        if auth:
            out = fixture(self.version, f"auth-status-{auth}", **override)
            (self.auth / f"{id}.json").write_text(json.dumps(out))
        self.accounts.append(f'[[account]]\nid = "{id}"\nplan = "{plan}"\nconfig_dir = "{d}"\n{extra}')
        (self.tmp / "accounts.toml").write_text("\n".join(self.accounts))
        return d

    def snap(self, id, five=None, seven=None, at=NOW):
        w = {}
        if five:
            w["five_hour"] = Window(five[0], NOW + five[1] * H)
        if seven:
            w["seven_day"] = Window(seven[0], NOW + seven[1] * H)
        store.save(id, Snapshot(at, w))

    def go(self, *argv, tty=False, answers=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch("builtins.input", side_effect=list(answers)) as asked, \
                mock.patch("os.execvpe") as launched:
            code = cli.main(list(argv))
        self.asked, self.launched = asked, launched
        return code, out.getvalue(), err.getvalue()

    def launched_dir(self):
        self.launched.assert_called_once()
        return self.launched.call_args.args[2]["CLAUDE_CONFIG_DIR"]

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
        self.assertIn("week 99% used", lines["a"])
        self.assertIn("usable at 11:00 (in 3h00m)", lines["a"])
        self.assertIn("5h window 95% used", lines["b"])
        self.assertIn("usable at 13:00 (in 5h00m)", lines["b"])
        self.assertEqual(out, "")

    def test_a_far_off_time_carries_the_day(self):
        self.add("a", orgId=ORG + "a")
        self.snap("a", five=(10, 1), seven=(99, 30))
        _, _, err = self.go("run")
        self.assertIn("usable at Sat 14:00 (in 30h00m)", err)

    def test_pick_keeps_exit_75_and_status_shows_when(self):
        self.spent_pair()
        self.assertEqual(self.go("pick")[0], 75)
        code, out, _ = self.go("status")
        self.assertEqual(code, 0)
        self.assertIn("usable at 13:00", [l for l in out.splitlines() if " b " in l][0])
        self.assertIn("usable at 11:00", [l for l in out.splitlines() if " a " in l][0])

    def test_eligible_top_launches_with_no_prompt_and_no_warning(self):
        self.add("a")
        self.snap("a", five=(10, 1), seven=(40, 50))
        code, _, err = self.go("run", "--", "-p", "hi", tty=True)
        self.assertEqual(self.launched_dir(), str(self.tmp / "accts/a"))
        self.assertEqual(self.launched.call_args.args[1][1:], ["-p", "hi"])
        self.assertNotIn("warning", err)
        self.asked.assert_not_called()

    def test_hot_short_window_resetting_within_grace_warns_first(self):
        self.add("a")
        self.snap("a", five=(95, 0.5), seven=(40, 50))
        _, _, err = self.go("run")
        self.assertEqual(self.launched_dir(), str(self.tmp / "accts/a"))
        self.assertIn("warning: a five_hour window 95% used, resets in 30m", err)


class Prompt(Fleet):
    def test_start_launches_the_top_ranked_not_the_soonest(self):
        self.spent_pair()
        code, _, err = self.go("run", tty=True, answers=["s"])
        self.assertEqual(self.launched_dir(), str(self.tmp / "accts/b"))
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
        self.assertEqual(self.launched_dir(), str(self.tmp / "accts/a"))

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
        self.assertEqual(self.go("pick")[1].strip(), "ok")

    def test_run_prints_one_line_about_what_it_skipped(self):
        _, _, err = self.go("run")
        skipped = [l for l in err.splitlines() if "skipped" in l]
        self.assertEqual(len(skipped), 1)
        self.assertIn("out (not signed in)", skipped[0])
        self.assertIn("key (not a subscription login)", skipped[0])
        self.assertEqual(self.launched_dir(), str(self.tmp / "accts/ok"))

    def test_nothing_rankable_is_an_error_not_a_launch(self):
        (self.tmp / "accounts.toml").write_text(self.accounts[1])
        with self.assertRaises(SystemExit) as e, mock.patch("os.execvpe") as launched, \
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
        self.assertIn("week 30% used", s.verdicts[0].reason)
        self.assertEqual([(a.id, rep) for a, rep in s.twins], [("one", "two")])

    def test_status_shows_the_other_folder_as_the_same_login(self):
        _, out, _ = self.go("status")
        self.assertIn("one", [l for l in out.splitlines() if "same login as two" in l][0])
        self.assertEqual(len([l for l in out.splitlines() if "score" in l]), 2)

    def test_nothing_identifying_is_printed(self):
        for cmd in ("status", "pick", "run"):
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

    def test_init_proposes_the_detected_plan_instead_of_the_default(self):
        d = self.tmp / "home" / ".claude-new"
        d.mkdir()
        (d / claude.LOGIN_FILE).write_text("")
        (self.auth / ".claude-new.json").write_text(json.dumps(fixture(self.version, "auth-status-pro")))
        with mock.patch.object(claude, "default_plan", "claude-ultra"):
            found = cli._candidates({}, self.tmp / "home")
        self.assertEqual([(a["id"], a["plan"]) for a, _ in found], [("claude-new", "claude-pro")])
        (self.auth / ".claude-new.json").unlink()          # no answer: the default stands
        with mock.patch.object(claude, "default_plan", "claude-ultra"):
            found = cli._candidates({}, self.tmp / "home")
        self.assertEqual([(a["id"], a["plan"]) for a, _ in found], [("claude-new", "claude-ultra")])


class ProbeFails(Fleet):
    def test_falls_back_to_the_login_file_and_the_configured_plan(self):
        self.add("a", plan="claude-max", auth=None)           # claude exits non-zero
        self.add("b", auth=None, login=False)
        self.add("c", orgId=ORG + "c")
        s = cli._survey()
        self.assertEqual({v.account.id: v.account.plan.name for v in s.verdicts},
                         {"a": "claude-max", "c": "claude-pro"})
        self.assertEqual([(a.id, why) for a, why in s.out], [("b", "not signed in")])
        self.assertEqual(s.failed, ["a", "b"])

    def test_status_says_what_failed_and_what_was_used(self):
        self.add("a", auth=None)
        self.add("b", auth=None, login=False)
        out = self.go("status")[1]
        self.assertIn("sign-in check failed; login file found, counted as signed in; "
                      "plan claude-pro from config", out)
        self.assertIn("login file not found, counted as signed out", out)

    def test_run_prints_one_line_and_still_launches(self):
        self.add("a", auth=None)
        self.add("b", auth=None)
        _, _, err = self.go("run")
        self.assertEqual(len([l for l in err.splitlines() if "sign-in check failed" in l]), 1)
        self.assertIn("a, b", err)
        self.launched.assert_called_once()

    def test_same_login_is_not_grouped_when_the_check_failed(self):
        self.add("a", auth=None)
        self.add("b", auth=None)
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
    def test_the_environment_is_scrubbed_and_points_at_the_account(self):
        d = self.add("a")
        hostile = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t",
                   "CLAUDE_CODE_OAUTH_TOKEN": "o", "CLAUDECODE": "1", "CLAUDE_PID": "9",
                   "CLAUDE_EFFORT": "max", "CLAUDE_CODE_ENTRYPOINT": "cli",
                   "CLAUDE_CODE_SOMETHING_NEW": "x", "CLAUDE_CONFIG_DIR": "/somewhere/else",
                   "KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, hostile):
            status = claude.account_status(Account("a", "claude", load_plan("claude-pro"), d))
        self.assertTrue(status.logged_in)
        seen = dict(l.split("=", 1) for l in (self.log / "env-a").read_text().splitlines() if "=" in l)
        for k in hostile:
            if k not in ("KEEP_ME", "CLAUDE_CONFIG_DIR"):
                self.assertNotIn(k, seen)
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], str(d))
        self.assertEqual(seen["KEEP_ME"], "yes")

    def test_clean_env_is_the_one_helper_for_children(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_X": "1", "ANTHROPIC_API_KEY": "k"}):
            env = claude.clean_env(Path("/acct"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/acct")
        self.assertNotIn("CLAUDE_CODE_X", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["HOME"], os.environ["HOME"])

    def test_default_command_and_the_per_account_override(self):
        self.add("a")
        self.add("b", extra='status_args = "whoami --text"\n')
        accounts, _ = load_accounts(self.tmp / "accounts.toml")
        self.assertEqual([a.status_args for a in accounts], [None, ("whoami", "--text")])
        for a in accounts:
            claude.account_status(a)
        self.assertEqual((self.log / "args-a").read_text().split(), ["auth", "status", "--json"])
        self.assertEqual((self.log / "args-b").read_text().split(), ["whoami", "--text"])

    def test_the_override_survives_init_rewriting_the_file(self):
        text = render_config([{"id": "b", "plan": "claude-pro", "config_dir": "/x",
                               "status_args": "whoami --text"}], {})
        p = self.tmp / "round.toml"
        p.write_text(text)
        self.assertEqual(load_accounts(p)[0][0].status_args, ("whoami", "--text"))

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
                    shutil.copy(v / f"{name}.json", self.auth / "a.json")
                    self.assertEqual(claude.account_status(acct).logged_in, expect)

    def test_an_unknown_shape_is_a_failed_probe(self):
        for raw in ({}, {"loggedIn": "yes"}, [], None, "x"):
            self.assertIsNone(claude.parse_account_status(raw))


if __name__ == "__main__":
    unittest.main()
