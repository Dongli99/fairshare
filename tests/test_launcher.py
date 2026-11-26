"""The launcher, `run --account`, `doctor`, plans without a short window, and the adapter
boundary. Same fleet as test_honest: temp HOME, temp PATH, a fake claude and a fake fairshare;
the real ones are never run (Fleet.setUp proves the fakes come first on PATH)."""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, model, policy, session
from fairshare.model import Account, Snapshot, Window, load_plan
from fairshare.providers import claude

from test_honest import H, NOW, ORG, Fleet
from test_hub import own_hooks


def args_seen(fleet, id):
    return (fleet.log / f"args-{id}").read_text().split()


def env_seen(fleet, id):
    return dict(l.split("=", 1) for l in (fleet.log / f"env-{id}").read_text().splitlines() if "=" in l)


class Launch(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a")
        self.snap("a", five=(10, 1), seven=(40, 50))

    def test_no_arguments_starts_a_session_with_no_arguments(self):
        code, _, err = self.go(real=True)
        self.assertEqual(code, 0)
        self.assertIn("fairshare -> a", err)
        self.assertEqual(args_seen(self, "a"), [])                 # the session, not the probe
        self.assertEqual(env_seen(self, "a")["CLAUDE_CONFIG_DIR"], str(self.tmp / "hub/accounts/a"))

    def test_run_passes_its_arguments_unchanged(self):
        for argv in (["run", "-p", "hi"], ["run", "--", "-p", "hi"], ["run", "-c"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.go(*argv, real=True)[0], 0)
                self.assertEqual(args_seen(self, "a"), [x for x in argv[1:] if x != "--"])

    def test_the_prompt_as_first_argument_is_not_an_account_id(self):
        self.go("run", "work", "-p", "x", real=True)
        self.assertEqual(args_seen(self, "a"), ["work", "-p", "x"])

    def test_only_a_leading_account_flag_is_ours(self):
        self.go("run", "--", "-a", "a", real=True)
        self.assertEqual(args_seen(self, "a"), ["-a", "a"])
        self.go("run", "-p", "hi", "-a", "a", real=True)
        self.assertEqual(args_seen(self, "a"), ["-p", "hi", "-a", "a"])

    def test_the_exit_code_of_the_session_is_ours(self):
        (self.bin / "claude").write_text(
            '#!/bin/sh\n[ "$1" = auth ] && exec cat "$FAKE_AUTH/a.json"\nexit 42\n')
        self.assertEqual(self.go(real=True)[0], 42)
        self.assertEqual(self.go("run", "-p", "x", real=True)[0], 42)

    def test_the_session_runs_as_a_child_with_the_clean_environment(self):
        hostile = {"ANTHROPIC_API_KEY": "key-value", "CLAUDE_CODE_ENTRYPOINT": "cli",
                   "CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/somewhere/else", "KEEP_ME": "yes"}
        with mock.patch.dict("os.environ", hostile), mock.patch("os.execvpe") as execed:
            self.go(real=True)
        execed.assert_not_called()                      # a child, not a replaced process
        cmd, env, _ = self.launched.call_args.args
        self.assertEqual(cmd, [str(self.bin / "claude")])
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        seen = env_seen(self, "a")
        self.assertEqual(seen["FAIRSHARE_LAUNCHER"], str(os.getpid()))   # the launcher: us
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
            self.assertNotIn(k, seen)
        self.assertEqual((seen["CLAUDE_CONFIG_DIR"], seen["KEEP_ME"]),
                         (str(self.tmp / "hub/accounts/a"), "yes"))

    def test_dropped_login_variables_are_named_once_and_never_valued(self):
        hostile = {"ANTHROPIC_API_KEY": "key-value-1", "ANTHROPIC_AUTH_TOKEN": "tok-value-2",
                   "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value-3"}
        with mock.patch.dict("os.environ", hostile):
            _, out, err = self.go(real=True)
        lines = [l for l in err.splitlines() if "ignoring" in l]
        self.assertEqual(len(lines), 1)
        for name, value in hostile.items():
            self.assertIn(name, lines[0])
            self.assertNotIn(value, out + err)
        self.assertNotIn("ignoring", self.go(real=True)[2])        # nothing set: no notice

    def test_the_notice_names_what_the_adapter_says(self):
        self.assertEqual(claude.dropped_login_vars(), [])
        with mock.patch.dict("os.environ", {"ANTHROPIC_AUTH_TOKEN": "x"}):
            self.assertEqual(claude.dropped_login_vars(), ["ANTHROPIC_AUTH_TOKEN"])

    def test_routing_still_refuses_before_any_child_starts(self):
        self.snap("a", five=(10, 1), seven=(99, 3))
        self.assertEqual(self.go()[0], 75)
        self.launched.assert_not_called()


class Account_(Fleet):
    """`run --account`: the user's explicit choice."""

    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", five=(10, 1), seven=(40, 50))
        self.snap("b", five=(10, 1), seven=(10, 50))      # b ranks first

    def test_it_launches_that_account_and_skips_ranking(self):
        for flag in ("-a", "--account"):
            with self.subTest(flag=flag):
                self.go("run", flag, "a", "-c", real=True)
                self.assertEqual(env_seen(self, "a")["CLAUDE_CONFIG_DIR"], str(self.tmp / "hub/accounts/a"))
                self.assertEqual(args_seen(self, "a"), ["-c"])
        self.go("run", "--account=a", "--", "-a", "x", real=True)
        self.assertEqual(args_seen(self, "a"), ["-a", "x"])

    def test_it_launches_even_when_the_account_is_near_a_limit_with_one_warning(self):
        self.snap("a", five=(10, 1), seven=(99, 3))
        self.snap("b", five=(95, 5), seven=(50, 100))
        for who, reason, usable in (("a", "seven_day 99% used", "usable at 11:00"),
                                    ("b", "five_hour 95% used", "usable at 12:00")):
            code, out, err = self.go("run", "-a", who, tty=True)
            self.assertEqual(code, 0)
            self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts" / who))
            warnings = [l for l in err.splitlines() if "warning" in l]
            self.assertEqual(len(warnings), 1)
            self.assertIn(reason, warnings[0])
            self.assertIn(usable, warnings[0])
            self.asked.assert_not_called()                    # no prompt, no refusal

    def test_an_account_with_room_launches_without_a_warning(self):
        _, _, err = self.go("run", "-a", "a")
        self.assertNotIn("warning", err)

    def test_an_unknown_id_lists_the_configured_ones_and_exits_2(self):
        code, _, err = self.go("run", "-a", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no account 'nope' (configured: a, b)", err)
        self.launched.assert_not_called()

    def test_a_missing_id_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as e, mock.patch("sys.stderr"):
            self.go("run", "-a")
        self.assertEqual(e.exception.code, 2)

    def test_not_signed_in_and_not_a_subscription_stop_with_exit_1(self):
        self.add("out", auth="logged-out")
        self.add("key", authMethod="api-key", orgId=ORG + "key")
        for who, why in (("out", "not signed in"), ("key", "not a subscription login")):
            with self.subTest(account=who):
                code, _, err = self.go("run", "-a", who)
                self.assertEqual(code, 1)
                self.assertIn(why, err)
                self.launched.assert_not_called()

    def test_a_failed_check_falls_back_to_the_last_confirmation(self):
        self.add("c", auth=None)                    # the check fails, confirmed before
        self.add("d", auth=None)                    # the check fails, confirmed as not one
        self.confirm("c")
        self.confirm("d", False)
        code, _, err = self.go("run", "-a", "c")
        self.assertEqual(code, 0)
        self.assertIn("sign-in check failed for c; went by its last confirmation", err)
        self.launched.assert_called_once()
        code, _, err = self.go("run", "-a", "d")
        self.assertEqual(code, 1)
        self.assertIn("d was never confirmed as a subscription login", err)
        self.launched.assert_not_called()

    def test_only_that_account_is_asked(self):
        with mock.patch.object(claude, "account_status", wraps=claude.account_status) as spy:
            self.go("run", "-a", "a")
        self.assertEqual(spy.call_count, 1)

    def test_nothing_identifying_is_printed(self):
        self.snap("a", five=(10, 1), seven=(99, 3))
        _, out, err = self.go("run", "-a", "a")
        for secret in ("someone@example", "person@example", ORG, "Example Org"):
            self.assertNotIn(secret, out + err)


class Doctor(Fleet):
    def lines(self, *argv):
        code, out, err = self.go(*argv or ("doctor",))
        return code, out.splitlines()

    def wire(self, command, hook=True):
        """The hub's one settings file, which every account links to: the usage hook, and the
        stop-failure hook running the same fairshare command (unless not `hook`)."""
        d = self.tmp / "hub" / "shared"
        d.mkdir(parents=True, exist_ok=True)
        runner = command.partition(" capture")[0]
        hooks = own_hooks(runner)
        (d / "settings.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": command}, **({"hooks": hooks} if hook
                                                                      else {})}))
        dest, text = claude.skill(d)                     # and fairshare's skill, current
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

    def healthy(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.serve_usage("a")
        self.serve_usage("b")
        self.wire(f"{self.bin}/fairshare capture")

    def test_everything_passing_exits_0_with_one_line_per_check(self):
        self.healthy()
        code, lines = self.lines()
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, [
            "pass  claude: command found, version 9.9.9 (Fake Code)",
            "pass  a: sign-in check answers: signed in until 2030-01-02, plan claude-pro",
            "pass  a: usage probe answers (five_hour, seven_day, 1 model week), local command",
            "pass  a: no state file yet",
            "pass  b: sign-in check answers: signed in until 2030-01-02, plan claude-pro",
            "pass  b: usage probe answers (five_hour, seven_day, 1 model week), local command",
            "pass  b: no state file yet",
            "pass  claude: usage hook in the hub calls fairshare capture",
            "pass  claude: hooks in the hub (stop-failure, post-tool, prompt, stop) call "
            "fairshare hook",
            "pass  claude: skill in the hub is current",
            "pass  fairshare command (called by the claude usage hook) runs"])

    def test_a_missing_binary_fails(self):
        self.healthy()
        (self.bin / "claude").unlink()
        with mock.patch.dict("os.environ", {"PATH": f"{self.bin}:/usr/bin:/bin"}):
            code, lines = self.lines()
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "FAIL  claude: command not found on PATH")
        self.assertIn("FAIL  a: sign-in check gave no answer", lines)

    def test_an_unanswered_sign_in_check_fails_that_account_only(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", auth=None)
        self.wire(f"{self.bin}/fairshare capture")
        code, lines = self.lines()
        self.assertEqual(code, 1)
        self.assertIn("pass  a: sign-in check answers: signed in until 2030-01-02, plan claude-pro",
                      lines)
        self.assertIn("FAIL  b: sign-in check gave no answer", lines)

    def test_a_signed_out_account_still_answers(self):
        self.add("a", auth="logged-out")
        self.wire(f"{self.bin}/fairshare capture")
        self.assertIn("pass  a: sign-in check answers: signed out", self.lines()[1])

    def test_a_hub_statusline_that_does_not_call_capture_fails(self):
        self.healthy()
        for broken in ("~/bin/line.sh", None):
            with self.subTest(statusline=broken):
                if broken:
                    self.wire(broken)
                else:
                    (self.tmp / "hub/shared/settings.json").unlink()
                code, lines = self.lines()
                self.assertEqual(code, 1)
                self.assertIn("FAIL  claude: usage hook in the hub does not call fairshare "
                              "capture (run `fairshare setup`)", lines)

    def test_a_fairshare_command_that_does_not_run_fails(self):
        self.healthy()
        self.wire(f"{self.tmp}/gone/fairshare capture")
        code, lines = self.lines()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  fairshare command (called by the claude usage hook) runs", lines)

    def test_a_command_that_runs_but_fails_also_fails(self):
        self.healthy()
        (self.bin / "fairshare").write_text("#!/bin/sh\nexit 3\n")
        self.assertEqual(self.lines()[0], 1)

    def test_the_older_command_form_is_checked_too(self):
        self.add("a", orgId=ORG + "a")
        self.serve_usage("a")
        self.wire(f"{sys.executable} -I -c 'import sys; sys.exit(0)  # fairshare' capture a")
        code_, lines = self.lines()
        self.assertEqual(code_, 0, lines)
        self.assertIn("pass  fairshare command (called by the claude usage hook) runs", lines)

    def test_no_config_fails_and_still_checks_the_command(self):
        code, lines = self.lines()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  accounts.toml not found (run `fairshare setup`)", lines)
        self.assertIn("pass  fairshare command (installed) runs", lines)

    def test_it_changes_nothing_and_prints_nothing_identifying(self):
        self.healthy()
        before = {p: p.read_text() for p in self.tmp.rglob("*") if p.is_file()
                  and p.parent != self.log}
        code, out, err = self.go("doctor")
        for secret in ("someone@example", "person@example", ORG, "Example Org", str(self.tmp)):
            self.assertNotIn(secret, out + err)
        self.assertEqual({p: p.read_text() for p in self.tmp.rglob("*") if p.is_file()
                          and p.parent != self.log}, before)


class NoShortWindow(Fleet):
    """A plan may omit [windows.short]; it then ranks on the week alone."""
    PLAN = {"name": "weekly-only", "provider": "claude", "capacity": 2.0,
            "windows": {"long": {"name": "seven_day", "hours": 168}}}

    def setUp(self):
        super().setUp()
        real = model._plan_data
        p = mock.patch.object(model, "_plan_data",
                              side_effect=lambda n: self.PLAN if n == "weekly-only" else real(n))
        p.start()
        self.addCleanup(p.stop)

    def test_it_loads_with_no_short_name(self):
        plan = load_plan("weekly-only")
        self.assertEqual((plan.short_name, plan.long_name, plan.long_hours), (None, "seven_day", 168.0))
        self.assertEqual(load_plan("claude-ultra").short_name, "five_hour")   # shipped: still there

    def test_it_ranks_on_the_week_alone(self):
        weekly = Account("w", "claude", load_plan("weekly-only"), Path("/x"))
        pro = Account("p", "claude", load_plan("claude-pro"), Path("/x"))
        week = lambda used, hours: Window(used, NOW + hours * H)
        snaps = {"w": Snapshot(NOW, {"seven_day": week(50, 48)}),
                 "p": Snapshot(NOW, {"seven_day": week(50, 48), "five_hour": week(10, 3)})}
        v = {x.account.id: x for x in policy.rank([weekly, pro], snaps, NOW)}
        self.assertEqual(v["w"].score, 2 * v["p"].score)          # capacity 2 vs 1, same week
        self.assertTrue(v["w"].eligible)
        # a stray five-hour window in a snapshot does not matter to a plan that has none
        hot = Snapshot(NOW, {"seven_day": week(50, 48), "five_hour": week(99, 4)})
        self.assertTrue(policy.rank([weekly], {"w": hot}, NOW)[0].eligible)

    def test_the_cli_runs_and_shows_it(self):
        self.add("w", plan="weekly-only", planTier="other")
        self.snap("w", seven=(40, 50))
        code, out, _ = self.go("status")
        self.assertEqual(code, 0)
        self.assertIn("weekly-only", out)
        self.assertIn("seven_day 40% used", out)
        self.assertEqual(self.go("run", "-a", "w")[0], 0)
        self.assertEqual(self.go("run")[0], 0)
        self.launched.assert_called_once()

    def test_the_stage_copes_with_no_short_window(self):
        """A stray five-hour window does not matter; with no `per_short`, the week alone gives
        no room to count, so the plan never reaches the handoff stage."""
        acct = Account("w", "claude", load_plan("weekly-only"), Path("/x"))
        snap = Snapshot(NOW, {"five_hour": Window(99, NOW + 60), "seven_day": Window(99, NOW + H)})
        self.assertEqual(cli._stage(acct, snap, NOW), "continue")

    def test_the_short_window_length_is_plan_data_for_the_status_line(self):
        self.assertEqual(load_plan("claude-pro").short_hours, 5.0)
        self.assertIsNone(load_plan("weekly-only").short_hours)


class Boundary(unittest.TestCase):
    """Nothing in cli.py spells a fact about the provider; the adapter owns them all."""
    SRC = (Path(cli.__file__)).read_text()

    def test_no_claude_specific_name_is_left_in_cli(self):
        self.assertIsNone(re.search(r"claude|anthropic", self.SRC, re.I))   # of any case
        consts = [claude.BINARY, claude.CONFIG_DIR_ENV, claude.SETTINGS_FILE, claude.tool,
                  *claude.STATE_CHROME, claude.GIT_DIR, claude.GIT_COMMONDIR,
                  claude.STATUS_LINE_KEY, claude.SESSION_MARK_ENV,
                  claude.WATCH_ENV_PREFIX, claude.ACCOUNT_DIR_GLOB, *claude.LOGIN_OVERRIDE_ENV, *claude.SESSION_ENV, *claude.RC_FILES,
                  claude.STATE_FILE, claude.STATE_LOCK, claude.STATE_TRUST, claude.STATE_MCP,
                  *claude.STATE_ONBOARDING, *claude.SHARED]
        for c in consts:
            with self.subTest(constant=c):
                self.assertFalse(c in self.SRC, f"cli.py spells {c!r}")

    def test_cli_does_not_import_a_provider_module(self):
        self.assertNotIn("providers import", self.SRC)
        self.assertNotIn("providers.claude", self.SRC)

    def test_every_provider_implements_the_protocol(self):
        from fairshare.providers import REGISTRY, base
        for prov in REGISTRY.values():
            for name in (n for n in dir(base.Provider) if not n.startswith("_")):
                with self.subTest(provider=prov.name, member=name):
                    self.assertTrue(hasattr(prov, name))

    def test_the_removed_things_are_gone(self):
        for n in ("shim_script", "is_shim", "SHIM_MARK", "BLOCK_BEGIN", "BLOCK_END", "DISABLED",
                  "rc_file"):
            self.assertFalse(hasattr(claude, n), n)
        for n in ("_write_shim", "cmd_shim_install", "_self_command", "_shim_dir"):
            self.assertFalse(hasattr(cli, n), n)

    def test_nothing_writes_into_the_users_folders_any_more(self):
        for n in ("cmd_init", "cmd_apply", "_offer", "_candidates", "_files"):
            self.assertFalse(hasattr(cli, n), n)
        for n in ("unwire", "_wrap", "_unwrap", "_settings"):
            self.assertFalse(hasattr(claude, n), n)
        from fairshare import hub
        self.assertNotRegex(Path(hub.__file__).read_text(), r"(?i)claude|anthropic")

    def test_the_window_names_a_capture_may_carry_come_from_the_plan_files(self):
        self.assertEqual(model.window_names("claude"), {"five_hour", "seven_day"})
        with mock.patch.object(claude, "window_names", return_value={"month"}):
            w = claude.parse_capture({"rate_limits": {"month": {"used_percentage": 3, "resets_at": 9},
                                                      "seven_day": {"used_percentage": 3, "resets_at": 9}}})
        self.assertEqual(set(w), {"month"})


if __name__ == "__main__":
    unittest.main()
