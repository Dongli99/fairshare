"""Fix round 1: bad input and unknown names degrade instead of crashing; a login never confirmed
as a subscription is never fallen back on; the environment scrub is exact. Same fleet as
test_honest: temp HOME, temp PATH, fake claude and fairshare (Fleet.setUp proves it); the real
ones are never run, and nothing here prints an environment variable's value."""
import contextlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, model, store
from fairshare.model import ConfigError, load_accounts
from fairshare.providers import claude

from test_honest import EMAIL, NOW, ORG, Fleet
from test_launcher import args_seen, env_seen

KNOWN = "claude-max, claude-pro, claude-ultra"
PAYLOAD = {"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": NOW + 3600},
                           "seven_day": {"used_percentage": 30, "resets_at": NOW + 90000}}}


class UnknownPlan(Fleet):
    """F3: a plan name with no plan file leaves that account out; nothing else breaks."""

    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", plan="claude-max-5x", orgId=ORG + "b")
        self.snap("a", five=(10, 1), seven=(40, 50))
        self.reason = f"plan claude-max-5x not found; known: {KNOWN}"

    def test_it_loads_with_the_reason_and_the_providers_only_provider(self):
        accounts, *_ = load_accounts(self.tmp / "accounts.toml")
        b = accounts[1]
        self.assertEqual((b.plan, b.plan_error, b.provider), (None, self.reason, "claude"))
        self.assertIsNotNone(accounts[0].plan)

    def test_status_ranks_the_others_and_says_why_it_left_one_out(self):
        code, out, _ = self.go("status")
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith("> a "))
        self.assertIn(f"left out: {self.reason}", [l for l in out.splitlines() if " b " in l][0])

    def test_run_prints_one_line_and_launches_the_other(self):
        code, _, err = self.go("run")
        self.assertEqual(code, 0)
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))
        self.assertEqual([l for l in err.splitlines() if "skipped" in l],
                         [f"fairshare: skipped b ({self.reason})"])

    def test_run_account_works_for_the_other_and_explains_the_broken_one(self):
        self.assertEqual(self.go("run", "-a", "a")[0], 0)
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))
        code, _, err = self.go("run", "-a", "b")
        self.assertEqual(code, 1)
        self.assertIn(self.reason, err)
        self.launched.assert_not_called()

    def test_doctor_fails_that_account_and_checks_the_rest(self):
        (self.tmp / "hub/shared").mkdir(parents=True)
        (self.tmp / "hub/shared/settings.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": f"{self.bin}/fairshare capture"}}))
        code, out, _ = self.go("doctor")
        lines = out.splitlines()
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL  b: {self.reason}", lines)
        self.assertIn("pass  a: sign-in check answers: signed in, plan claude-pro", lines)
        self.assertIn("pass  claude: usage hook in the hub calls fairshare capture", lines)

    def test_capture_keeps_recording_for_every_account_the_broken_one_included(self):
        for who in ("a", "b"):
            with mock.patch.object(sys, "stdin",
                                   types.SimpleNamespace(buffer=io.BytesIO(json.dumps(PAYLOAD).encode()))), \
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts" / who)}):
                self.assertEqual(self.go("capture", "--then", "exit 7")[0], 7)
            self.assertEqual(sorted(store.load(who).windows), ["five_hour", "seven_day"], who)

    def test_with_no_provider_named_and_several_providers_it_is_a_config_error(self):
        other = {"name": "other", "provider": "elsewhere", "capacity": 1.0,
                 "windows": {"long": {"name": "month", "hours": 720}}}
        real = model._plan_data
        with mock.patch.object(model, "_plan_data", lambda n: other if n == "other" else real(n)), \
                mock.patch.object(model, "plan_names", lambda: ["claude-pro", "other"]):
            with self.assertRaises(ConfigError) as e:
                load_accounts(self.tmp / "accounts.toml")
        self.assertIn("account 2", str(e.exception))
        self.assertIn("no provider is named", str(e.exception))


class BadInput(Fleet):
    """F10: no tracebacks."""

    def write(self, text):
        (self.tmp / "accounts.toml").write_text(text)

    def test_a_bad_policy_key_names_the_key_and_the_valid_ones(self):
        self.write('[policy]\nshort_hot_percent = 90\n')
        for cmd in ("status", "run"):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit) as e:
                self.go(cmd)
            msg = str(e.exception.code)
            self.assertIn("short_hot_percent", msg)
            for valid in ("short_hot_pct", "long_exhausted_pct", "grace_hours", "min_hours"):
                self.assertIn(valid, msg)

    def test_a_policy_value_that_is_not_a_number_is_named(self):
        self.write('[policy]\ngrace_hours = "soon"\n')
        with self.assertRaises(SystemExit) as e:
            self.go("status")
        self.assertIn("grace_hours must be a number", str(e.exception.code))

    def test_a_broken_file_names_the_file_and_the_line(self):
        self.write('[[account]]\nid = "a"\nplan = \n')
        for cmd in ("status", "run"):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit) as e:
                self.go(cmd)
            msg = str(e.exception.code)
            self.assertIn(str(self.tmp / "accounts.toml"), msg)
            self.assertIn("line 3", msg)

    def test_an_account_missing_a_key_is_named(self):
        self.write('[[account]]\nplan = "claude-pro"\n')
        with self.assertRaises(SystemExit) as e:
            self.go("status")
        self.assertIn("account 1: missing 'id'", str(e.exception.code))

    def test_a_config_dir_from_before_the_hub_says_what_to_do(self):
        self.write('[[account]]\nid = "a"\nplan = "claude-pro"\nconfig_dir = "~/.claude-a"\n')
        with self.assertRaises(SystemExit) as e:
            self.go("status")
        self.assertIn("account 1: config_dir is no longer used", str(e.exception.code))
        self.assertIn("fairshare setup", str(e.exception.code))

    def test_an_id_that_is_not_a_folder_name_is_refused(self):
        for bad in ("../evil", "a/b", ""):
            with self.subTest(id=bad):
                self.write(f'[[account]]\nid = "{bad}"\nplan = "claude-pro"\n')
                with self.assertRaises(SystemExit) as e:
                    self.go("status")
                self.assertIn("account 1: id", str(e.exception.code))

    def test_doctor_reports_every_config_error_as_a_failure(self):
        for text, expect in (('[policy]\nshort_hot_percent = 90\n', "short_hot_percent"),
                             ('[[account]]\nid = "a"\nplan = \n', "line 3"),
                             ('[[account]]\nid = "a"\n', "missing 'plan'"),
                             ('[[account]]\nid = "a"\nplan = "claude-pro"\n'
                              'capacity = -1\n', "capacity must be positive"),
                             ('[[account]]\nid = "a"\nplan = "claude-pro"\n'
                              'status_args = "a \\"b"\n', "account 1")):
            with self.subTest(expect=expect):
                self.write(text)
                code, out, _ = self.go("doctor")
                self.assertEqual(code, 1)
                bad = [l for l in out.splitlines() if l.startswith("FAIL  accounts.toml: ")]
                self.assertEqual(len(bad), 1, out)
                self.assertIn(expect, bad[0])
                self.assertNotIn(str(self.tmp), out)           # the file's name, not its path

    def test_a_missing_file_still_reads_as_before(self):
        self.assertIn("FAIL  accounts.toml not found (run `fairshare setup`)", self.go("doctor")[1])

    def test_capture_survives_a_broken_file_and_still_runs_the_users_line(self):
        self.write('[[account]]\nid = "a"\nplan = \n')
        with mock.patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}"))):
            self.assertEqual(self.go("capture", "--then", "exit 7")[0], 7)


class Confirmed(Fleet):
    """F11: a failed sign-in check never makes a login count as a subscription on its own."""
    NEVER = "sign-in check failed and {} was never confirmed as a subscription login"

    def test_every_successful_check_records_a_yes_or_no_and_a_time_only(self):
        self.add("sub", orgId=ORG + "sub")
        self.add("key", authMethod="api-key", orgId=ORG + "key")
        self.add("out", auth="logged-out")
        self.go("status")
        self.assertEqual([store.subscription_seen(i) for i in ("sub", "key", "out")],
                         [(True, NOW), (False, NOW), (False, NOW)])
        for f in (self.tmp / "state" / "checks").glob("*.json"):
            self.assertEqual(set(json.loads(f.read_text())), {"subscription", "checked_at"})
            for secret in (EMAIL, ORG, "Example Org"):
                self.assertNotIn(secret, f.read_text())

    def test_a_failed_check_records_nothing(self):
        self.add("a", auth=None)
        self.go("status")
        self.assertIsNone(store.subscription_seen("a"))
        self.confirm("a", True, 5.0)
        self.go("status")
        self.assertEqual(store.subscription_seen("a"), (True, 5.0))

    def test_never_confirmed_is_left_out_in_status_and_run(self):
        self.add("ok", orgId=ORG + "ok")
        self.add("new", auth=None)                    # login file exists, check fails, never confirmed
        out = self.go("status")[1]
        self.assertIn("left out: " + self.NEVER.format("new"), out)
        code, _, err = self.go("run")
        self.assertIn("skipped new (" + self.NEVER.format("new") + ")", err)
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/ok"))

    def test_last_answer_no_or_login_file_gone_is_still_left_out(self):
        self.add("key", auth=None)
        self.add("gone", auth=None, login=False)
        self.confirm("key", False)
        self.confirm("gone", True)
        out = self.go("status")[1]
        self.assertIn("left out: " + self.NEVER.format("key"), out)
        self.assertIn("gone", [l for l in out.splitlines() if "not signed in" in l][0])

    def test_confirmed_once_it_can_be_used_when_the_check_later_fails(self):
        self.add("a", orgId=ORG + "a")
        self.snap("a", five=(10, 1), seven=(40, 50))
        self.assertIn("> a", self.go("status")[1])          # a successful check records the yes
        (self.auth / "a.json").unlink()                      # now the check fails
        out = self.go("status")[1]
        self.assertIn("> a", out)
        self.assertIn("last confirmed as a subscription login on 2027-01-15", out)

    def test_run_account_follows_the_same_rule(self):
        self.add("a", auth=None)
        code, _, err = self.go("run", "-a", "a")
        self.assertEqual(code, 1)
        self.assertIn(self.NEVER.format("a"), err)
        self.launched.assert_not_called()
        self.confirm("a")
        self.assertEqual(self.go("run", "-a", "a")[0], 0)
        self.launched.assert_called_once()

    def test_run_account_records_what_it_learned(self):
        self.add("a", authMethod="api-key")
        self.assertEqual(self.go("run", "-a", "a")[0], 1)
        self.assertEqual(store.subscription_seen("a"), (False, NOW))

    def test_a_state_dir_that_cannot_be_written_does_not_stop_status(self):
        self.add("a")
        with mock.patch.object(store, "_write_json", side_effect=PermissionError):
            self.assertEqual(self.go("status")[0], 0)


class Scrub(Fleet):
    """F12: the scrub is exact, and the launcher says what it dropped or passed on."""

    def setUp(self):
        super().setUp()
        self.add("a")
        self.snap("a", five=(10, 1), seven=(40, 50))

    def test_login_and_provider_overrides_are_dropped_with_one_notice_naming_each(self):
        names = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                 "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_API_KEY")
        with mock.patch.dict(os.environ, {n: "secret-value" for n in names}):
            _, out, err = self.go(real=True)
        (line,) = [l for l in err.splitlines() if "ignoring" in l]
        for n in names:
            self.assertIn(n, line)
            self.assertFalse(n in env_seen(self, "a"), f"{n} reached the child")
        self.assertNotIn("secret-value", out + err)

    def test_session_state_is_dropped_silently_and_user_settings_pass(self):
        with mock.patch.dict(os.environ, {"CLAUDE_JOB_DIR": "/j", "CLAUDE_CODE_SESSION_ID": "s",
                                          "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000"}):
            _, _, err = self.go(real=True)
        seen = env_seen(self, "a")
        self.assertFalse("CLAUDE_JOB_DIR" in seen or "CLAUDE_CODE_SESSION_ID" in seen)
        self.assertEqual(seen["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "1000")
        self.assertNotIn("CLAUDE_JOB_DIR", err)
        self.assertNotIn("ignoring", err)
        self.assertNotIn("passing", err)           # not inside a session: no safety-net line

    def test_inside_a_session_one_line_names_any_other_claude_variable_passed_on(self):
        env = {"CLAUDECODE": "1", "CLAUDE_PID": "9", "CLAUDE_JOB_DIR": "/j",
               "ANTHROPIC_API_KEY": "k", "CLAUDE_CONFIG_DIR": "/x",
               "CLAUDE_CODE_NEW_THING": "value-1", "CLAUDE_OTHER": "value-2"}
        with mock.patch.dict(os.environ, env):
            _, out, err = self.go(real=True)
        (line,) = [l for l in err.splitlines() if "passing" in l]
        self.assertIn("CLAUDE_CODE_NEW_THING, CLAUDE_OTHER", line)
        for known in ("CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_CONFIG_DIR", "CLAUDECODE"):
            self.assertNotIn(known, line)
        self.assertNotIn("value-", out + err)
        self.assertEqual(env_seen(self, "a")["CLAUDE_CODE_NEW_THING"], "value-1")

    def test_inside_a_session_with_nothing_new_there_is_no_line(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_PID": "9"}):
            _, _, err = self.go(real=True)
        self.assertNotIn("passing", err)

    def test_the_adapter_answers_it(self):
        self.assertEqual(claude.passed_session_vars(), [])
        with mock.patch.dict(os.environ, {"CLAUDE_X": "1"}):
            self.assertEqual(claude.passed_session_vars(), [])       # no session mark
            os.environ["CLAUDECODE"] = "1"
            self.assertEqual(claude.passed_session_vars(), ["CLAUDE_X"])


class Capacity(Fleet):
    """F13: a per-account capacity override survives plan detection."""

    def test_the_override_is_used_after_the_plan_is_detected(self):
        self.add("a", plan="claude-pro", extra="capacity = 3.0\n", planTier="max")
        self.snap("a", five=(10, 1), seven=(40, 50))
        (v,) = cli._survey().verdicts
        self.assertEqual((v.account.plan.name, v.account.plan.capacity), ("claude-max", 3.0))
        self.go("run", "-a", "a")
        self.assertEqual(self.launched.call_args.args[1]["CLAUDE_CONFIG_DIR"], str(self.tmp / "hub/accounts/a"))

    def test_without_an_override_the_detected_plans_capacity_applies(self):
        self.add("a", plan="claude-pro", planTier="max")
        self.assertEqual(cli._survey().verdicts[0].account.plan.capacity,
                         model.load_plan("claude-max").capacity)

    def test_run_account_keeps_the_override_too(self):
        self.add("a", plan="claude-pro", extra="capacity = 3.0\n", planTier="max")
        self.snap("a", five=(10, 1), seven=(40, 50))
        with mock.patch.object(cli, "_launch") as launch:
            launch.return_value = 0
            cli._run_account("a", [])
        self.assertEqual(launch.call_args.args[0].account.plan.capacity, 3.0)


class Smalls(Fleet):
    def test_an_unknown_first_word_suggests_run(self):
        for argv, hint in ((["-p", "hi"], "did you mean: fairshare run -p hi"),
                           (["fix", "the bug"], "did you mean: fairshare run fix 'the bug'")):
            with self.subTest(argv=argv):
                code, out, err = self.go(*argv)
                self.assertEqual(code, 2)
                self.assertIn(hint, err)
                self.launched.assert_not_called()

    def test_help_and_real_commands_are_not_caught_by_it(self):
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--help"])
        self.assertEqual(e.exception.code, 0)
        self.add("a")
        self.assertEqual(self.go("status")[0], 0)

    def test_pick_is_gone_but_run_still_exits_75_when_nothing_is_eligible(self):
        self.assertFalse(hasattr(cli, "cmd_pick"))
        code, _, err = self.go("pick")
        self.assertEqual(code, 2)
        self.assertNotIn("pick", cli.__doc__)
        self.spent_pair()
        self.assertEqual(self.go("run")[0], 75)

    def test_the_discovery_folder_pattern_is_a_named_constant(self):
        self.assertEqual(claude.ACCOUNT_DIR_GLOB, ".claude*")
        home = self.tmp / "home"
        for n in (".claude-x", ".other-y"):
            (home / n).mkdir()
            (home / n / claude.LOGIN_FILE).write_text("")
        with mock.patch.object(claude, "ACCOUNT_DIR_GLOB", ".other*"):
            self.assertEqual([c.id for c in claude.discover(home)], ["other-y"])


if __name__ == "__main__":
    unittest.main()
