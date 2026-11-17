"""Stage 4: usage at launch. The adapter's `/usage` probe and its parser (against every recorded
version that has usage fixtures), routing on its answer in `run`, `run -a` and `status`, per-model
weeks (warn, never rank), the sign-in's end, and doctor's lines. Same fleet as test_honest: temp
HOME, PATH, config and state, a fake claude and fairshare first on PATH (Fleet.setUp proves it);
the real ones are never run. "Now" is pinned to when the fixtures were recorded."""
import contextlib
import json
import os
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from fairshare import cli, store
from fairshare.model import (USAGE_MODEL_TURN, USAGE_NOT_UNDERSTOOD, USAGE_SIGNED_OUT, Account,
                             Snapshot, Tuning, Window, load_accounts, load_plan, model_window)
from fairshare.providers import claude
from unittest import mock

from test_honest import ORG, VERSIONS, Fleet

DENVER = ZoneInfo("America/Denver")
H = 3600.0


def at(y, mo, d, h, mi=0, zone=DENVER) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=zone).timestamp()


RECORDED = at(2026, 11, 11, 10, 50)        # when the planner recorded the fixtures (UTC-7)
USAGE_VERSIONS = [v for v in VERSIONS if (v / "usage.txt").exists()]
EXPECTED = {                                # the fixtures' windows, read in the zone they name
    "usage.txt": {"five_hour": (19, at(2026, 11, 11, 15, 40)),
                  "seven_day": (6, at(2026, 11, 17, 21)),
                  "seven_day:Fable": (5, at(2026, 11, 17, 21))},
    "usage-week-hot.txt": {"five_hour": (6, at(2026, 11, 11, 11)),
                           "seven_day": (96, at(2026, 11, 12, 3)),
                           "seven_day:Fable": (27, at(2026, 11, 12, 3))},
}
EXPECTED["usage-week-hot.json"] = EXPECTED["usage-week-hot.txt"]


@contextlib.contextmanager
def local(zone: str):
    """Local time is `zone` while the block runs."""
    try:
        with mock.patch.dict(os.environ, {"TZ": zone}):
            time.tzset()
            yield
    finally:
        time.tzset()


def windows(d):
    return {k: (w.used_pct, w.resets_at) for k, w in d.items()}


class Parse(unittest.TestCase):
    def test_every_recorded_fixture(self):
        self.assertTrue(USAGE_VERSIONS)
        for v in USAGE_VERSIONS:
            for name in ("usage.txt", "usage-week-hot.txt"):
                with self.subTest(version=v.name, fixture=name):
                    got = claude.parse_usage((v / name).read_text(), RECORDED)
                    self.assertEqual(windows(got), EXPECTED[name])

    def test_the_model_week_key_comes_from_the_one_helper(self):
        self.assertEqual(model_window("seven_day", "Fable"), "seven_day:Fable")

    def test_the_clock_only_form_of_2_1_321(self):
        """From the 2026-11-09 notes (no fixture was recorded): the reset was a clock time only,
        with no date or zone, and the minutes jittered ("8:59pm" then "9pm"). Local time; the
        next occurrence, except that a minute just gone is that reset, not tomorrow's."""
        with local("America/Denver"):
            got = claude.parse_usage("Current session: 40% used · resets 8:59pm\n"
                                         "Current week (all models): 12% used · resets 9pm\n",
                                     RECORDED)
            self.assertEqual(windows(got), {"five_hour": (40, at(2026, 11, 11, 20, 59)),
                                            "seven_day": (12, at(2026, 11, 11, 21))})
            late = at(2026, 11, 11, 21, 0) + 30          # half a minute after "8:59pm"
            self.assertEqual(claude.parse_usage("Current session: 1% used · resets 8:59pm",
                                                late)["five_hour"].resets_at,
                             at(2026, 11, 11, 20, 59))
            self.assertEqual(claude.parse_usage("Current session: 1% used · resets 9am",
                                                late)["five_hour"].resets_at,
                             at(2026, 11, 12, 9))

    def test_the_year_rolls_over(self):
        dec30 = at(2026, 12, 30, 12)
        got = claude.parse_usage("Current week (all models): 50% used · resets Jan 2 at 3am "
                                 "(America/Denver)", dec30)
        self.assertEqual(got["seven_day"].resets_at, at(2027, 1, 2, 3))

    def test_extra_spaces_and_a_missing_used_are_tolerated(self):
        got = claude.parse_usage("  Current  session:  19 %  ·  resets Nov 11 at 3:40pm "
                                 "(America/Denver)\nCurrent week ( Fable ): 5% · resets Nov 17 "
                                 "at 9pm (America/Denver)", RECORDED)
        self.assertEqual(windows(got), {"five_hour": (19, at(2026, 11, 11, 15, 40)),
                                        "seven_day:Fable": (5, at(2026, 11, 17, 21))})

    def test_an_unreadable_reset_or_none_drops_that_line_only(self):
        text = ("Current session: 19% used · resets whenever\n"
                "Current week (all models): 6% used\n"
                "Current week (Fable): 5% used · resets Feb 30 at 9pm (America/Denver)\n"
                "Current week (Opus): 7% used · resets Nov 17 at 9pm (America/Denver)\n")
        self.assertEqual(windows(claude.parse_usage(text, RECORDED)),
                         {"seven_day:Opus": (7, at(2026, 11, 17, 21))})

    def test_an_unknown_zone_is_read_as_local_time(self):
        with local("Asia/Tokyo"):
            got = claude.parse_usage("Current session: 1% used · resets Nov 12 at 9am "
                                     "(Mars/Olympus)", RECORDED)
        self.assertEqual(got["five_hour"].resets_at,
                         at(2026, 11, 12, 9, zone=ZoneInfo("Asia/Tokyo")))

    def test_text_with_no_window_line_reads_nothing(self):
        self.assertEqual(claude.parse_usage("You are currently using your subscription\n\n"
                                            "What's contributing to your usage?\n", RECORDED), {})


class Usage(Fleet):
    """The fleet, pinned to when and where the fixtures were recorded (local time UTC-7)."""

    def setUp(self):
        super().setUp()
        self.clock[0] = RECORDED
        tz = mock.patch.dict(os.environ, {"TZ": "America/Denver"})
        tz.start()
        self.addCleanup(time.tzset)
        self.addCleanup(tz.stop)            # runs first: the zone is restored, then applied
        time.tzset()

    def snap(self, id, five=None, seven=None, at=RECORDED):
        """As Fleet.snap, with resets counted in hours from RECORDED."""
        w = {}
        if five:
            w["five_hour"] = Window(five[0], RECORDED + five[1] * H)
        if seven:
            w["seven_day"] = Window(seven[0], RECORDED + seven[1] * H)
        store.save(id, Snapshot(at, w))

    def serve_text(self, id, text, code=0):
        (self.auth / f"{id}.usage").write_text(text)
        (self.auth / f"{id}.usage-exit").write_text(str(code))

    def envelope(self, id, **override):
        d = {**json.loads((self.version / "usage-week-hot.json").read_text()), **override}
        self.serve_text(id, json.dumps({k: v for k, v in d.items() if v is not None}))

    def acct(self, id):
        return next(a for a in load_accounts(self.tmp / "accounts.toml")[0] if a.id == id)

    def probed(self, id):
        """The argument lists the fake saw for `id`'s usage probe, one per call."""
        p = self.log / f"usage-{id}"
        return p.read_text().split("/usage\n")[:-1] if p.exists() else []


class Probe(Usage):
    def test_the_envelope_fixture_through_the_fake(self):
        self.add("a")
        self.serve_usage("a")
        self.assertEqual(windows(claude.read_usage(self.acct("a"), RECORDED)),
                         EXPECTED["usage-week-hot.json"])

    def test_it_runs_the_documented_command_like_the_sign_in_check(self):
        d = self.add("a")
        self.serve_usage("a")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "CLAUDECODE": "1",
                                          "KEEP_ME": "yes"}):
            claude.read_usage(self.acct("a"), RECORDED)
        self.assertEqual((self.log / "usage-a").read_text().splitlines(),
                         ["-p", "--no-session-persistence", "--output-format", "json",
                          "--settings", '{"disableAllHooks":true}', "/usage"])
        seen = dict(l.split("=", 1) for l in (self.log / "usage-env-a").read_text().splitlines()
                    if "=" in l)
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], str(d))
        self.assertEqual(seen["KEEP_ME"], "yes")
        self.assertFalse("ANTHROPIC_API_KEY" in seen or "CLAUDECODE" in seen)

    def test_signed_out_comes_from_the_recorded_answer(self):
        self.add("a", auth="logged-out")
        self.serve_usage("a", "usage-logged-out.json",
                         int((self.version / "usage-logged-out.exit").read_text()))
        self.assertEqual(claude.read_usage(self.acct("a"), RECORDED), USAGE_SIGNED_OUT)

    def test_text_with_no_window_line_is_not_understood(self):
        self.add("a")
        self.envelope("a", result="You are currently using your subscription\n")
        self.assertEqual(claude.read_usage(self.acct("a"), RECORDED), USAGE_NOT_UNDERSTOOD)

    def test_a_model_turn_takes_no_windows(self):
        self.add("a")
        for override in ({"num_turns": 1}, {"local_command": None, "num_turns": 2},
                         {"total_cost_usd": 0.01}):
            with self.subTest(**override):
                self.envelope("a", **override)
                self.assertEqual(claude.read_usage(self.acct("a"), RECORDED), USAGE_MODEL_TURN)

    def test_an_error_or_unknown_envelope_is_not_called_a_model_turn(self):
        """Only turns or cost prove the model ran; a missing field alone must not tell the user
        to switch the probe off."""
        self.add("a")
        for override, want in (({"local_command": None, "is_error": True}, "no answer: error"),
                               ({"local_command": None}, USAGE_NOT_UNDERSTOOD),
                               ({"local_command": "other"}, USAGE_NOT_UNDERSTOOD)):
            with self.subTest(**override):
                self.envelope("a", **override)
                self.assertEqual(claude.read_usage(self.acct("a"), RECORDED), want)

    def test_no_answer_and_its_detail(self):
        self.add("a")
        acct = self.acct("a")
        with mock.patch.object(claude, "USAGE_TIMEOUT", 0.3), \
                mock.patch.dict(os.environ, {"FAKE_USAGE_SLEEP": "5"}):
            t = time.monotonic()
            self.assertEqual(claude.read_usage(acct, RECORDED), "no answer: timed out")
            self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(claude.read_usage(acct, RECORDED), "no answer: exit 1")   # nothing served
        self.serve_text("a", "")
        self.assertEqual(claude.read_usage(acct, RECORDED), "no answer: no output")
        self.envelope("a", is_error=True)
        self.assertEqual(claude.read_usage(acct, RECORDED), "no answer: error")
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertEqual(claude.read_usage(acct, RECORDED), "no answer: could not run claude")

    def test_usage_args_override_and_plain_text(self):
        self.add("a", extra='usage_args = "-p /usage"\n')
        self.add("b", extra='usage_args = ["-p", "/usage"]\n')
        for i in "ab":
            self.serve_text(i, (self.version / "usage.txt").read_text())
            with self.subTest(account=i):
                self.assertEqual(self.acct(i).usage_args, ("-p", "/usage"))
                self.assertEqual(windows(claude.read_usage(self.acct(i), RECORDED)),
                                 EXPECTED["usage.txt"])
                self.assertEqual(self.probed(i), ["-p\n"])

    def test_empty_usage_args_turn_it_off_and_no_call_is_made(self):
        self.add("a", orgId=ORG + "a", extra="usage_args = []\n")
        self.serve_usage("a")
        self.assertIsNone(claude.read_usage(self.acct("a"), RECORDED))
        self.snap("a", seven=(10, 50), at=RECORDED - H)
        code, out, _ = self.go("status")
        _, _, err = self.go("run")
        self.assertEqual(self.probed("a"), [])
        self.assertNotIn("usage probe", out + err)
        self.assertIn("(usage as of 09:50)", out)     # its numbers did not come from a probe
        self.assertIn("pass  a: usage probe off (usage_args = [])",
                      self.go("doctor")[1].splitlines())

    def test_bad_usage_args_are_a_config_error(self):
        self.add("a", extra="usage_args = [1]\n")
        with self.assertRaises(SystemExit) as e:
            self.go("status")
        self.assertIn("usage_args of a must be a string or a list of strings", str(e.exception))


class Route(Usage):
    def test_probes_run_in_parallel_with_the_sign_in_checks(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        with mock.patch.object(claude, "USAGE_TIMEOUT", 0.6), \
                mock.patch.object(claude, "PROBE_TIMEOUT", 0.6), \
                mock.patch.dict(os.environ, {"FAKE_SLEEP": "5", "FAKE_USAGE_SLEEP": "5"}):
            t = time.monotonic()
            cli._survey()
            took = time.monotonic() - t
        self.assertLess(took, 1.1)              # four slow answers, one timeout's wait
        self.assertEqual(len(self.probed("a")) + len(self.probed("b")), 2)

    def test_a_probe_is_recorded_and_ranks_over_an_older_snapshot(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", seven=(10, 30), at=RECORDED - 5 * H)
        self.snap("b", seven=(50, 100), at=RECORDED - 5 * H)
        self.assertEqual([v.account.id for v in cli._survey().verdicts], ["a", "b"])
        self.serve_usage("a")                   # a's week is in fact 96% used
        s = cli._survey()
        self.assertEqual([v.account.id for v in s.verdicts], ["b", "a"])
        snap = store.load("a")
        self.assertEqual(snap.captured_at, RECORDED)
        self.assertEqual(windows(snap.windows)["seven_day"], (96, at(2026, 11, 12, 3)))
        self.assertEqual(windows(snap.windows)["seven_day:Fable"], (27, at(2026, 11, 12, 3)))
        self.assertEqual(s.usage, {"a": None, "b": "no answer: exit 1"})

    def test_a_lower_probed_value_for_the_same_window_does_not_take_over(self):
        self.add("a")
        exact = at(2026, 11, 12, 3) + 40        # the statusLine's exact reset; /usage's minute
        store.save("a", Snapshot(RECORDED - H, {"seven_day": Window(97, exact)}))
        self.serve_usage("a")                   # 96%, reset printed a minute off
        cli._survey()
        snap = store.load("a")
        self.assertEqual(windows(snap.windows)["seven_day"], (97, exact))
        self.assertEqual(windows(snap.windows)["five_hour"], (6, at(2026, 11, 11, 11)))

    def test_failure_lines_and_usage_as_of(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.add("c", orgId=ORG + "c")
        self.serve_usage("a")
        self.snap("b", seven=(10, 50), at=RECORDED - 2 * H)       # c has no snapshot
        _, _, err = self.go("run")
        self.assertIn("fairshare: usage probe failed for b (no answer: exit 1); using its "
                      "snapshot from 08:50", err.splitlines())
        self.assertIn("fairshare: usage probe failed for c (no answer: exit 1); no snapshot; "
                      "ranked as a fresh window", err.splitlines())
        self.assertNotIn("for a", err)
        self.launched.assert_called_once()      # a failed probe never stops a launch
        out = self.go("status")[1]
        rows = {l.split()[1] if l.startswith(">") else l.split()[0]: l
                for l in out.splitlines() if not l.startswith("    ")}
        self.assertNotIn("usage as of", rows["a"])                # probed: nothing extra
        self.assertIn("(usage as of 08:50)", rows["b"])
        self.assertNotIn("usage as of", rows["c"])                # nothing to date
        self.assertIn("    usage probe failed for b (no answer: exit 1); using its snapshot from "
                      "08:50", out.splitlines())

    def test_a_model_turn_is_said_loudly_with_the_way_to_stop_it(self):
        self.add("a")
        self.envelope("a", local_command=None, num_turns=1)
        self.snap("a", seven=(10, 50), at=RECORDED - H)
        _, _, err = self.go("run")
        line = next(l for l in err.splitlines() if "model turn" in l)
        self.assertIn("warning:", line)
        self.assertIn("`usage_args = []`", line)
        self.assertIn("using its snapshot from 09:50", line)
        self.assertEqual(windows(store.load("a").windows), {"seven_day": (10, RECORDED + 50 * H)})
        self.launched.assert_called_once()
        self.assertIn("model turn", self.go("status")[1])
        self.assertIn(f"FAIL  a: usage probe failed ({USAGE_MODEL_TURN})",
                      self.go("doctor")[1].splitlines())

    def test_run_a_probes_records_and_says_why_it_failed(self):
        self.add("a")
        self.serve_usage("a")
        self.assertEqual(self.go("run", "-a", "a")[0], 0)
        self.assertEqual(windows(store.load("a").windows)["seven_day"][0], 96)
        (self.auth / "a.usage").unlink()
        _, _, err = self.go("run", "-a", "a")
        self.assertIn("usage probe failed for a (no answer: exit 1); using its snapshot from "
                      "10:50", err)

    def test_an_answer_that_cannot_be_recorded_is_said(self):
        self.add("a")
        self.serve_usage("a")
        with mock.patch.object(store, "record",
                               side_effect=PermissionError(13, "Permission denied")):
            _, _, err = self.go("run")
        self.assertIn("usage probe failed for a (not recorded: Permission denied); no snapshot; "
                      "ranked as a fresh window", err)
        self.launched.assert_called_once()

    def test_twins_are_each_probed(self):
        self.add("a")
        self.add("b")                           # the same login: one quota
        self.serve_usage("a")
        self.serve_usage("b")
        s = cli._survey()
        self.assertEqual(len(s.verdicts), 1)
        self.assertEqual((len(self.probed("a")), len(self.probed("b"))), (1, 1))
        self.assertTrue(store.load("a") and store.load("b"))


class ModelWeeks(Usage):
    def spent_fable(self, id):
        text = (self.version / "usage-week-hot.txt").read_text()
        self.serve_text(id, text.replace("Current week (all models): 96%",
                                         "Current week (all models): 40%")
                        .replace("Current week (Fable): 27%", "Current week (Fable): 99%"))

    def test_a_spent_model_week_warns_at_launch_and_does_not_change_the_ranking(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.spent_fable("a")
        self.snap("b", seven=(40, 100), at=RECORDED)
        _, _, err = self.go("run")
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))
        self.assertIn("fairshare: warning: a's Fable week is spent until Thu 03:00; other models "
                      "are unaffected", err.splitlines())
        out = self.go("status")[1]
        self.assertIn("    warning: a's Fable week is spent until Thu 03:00; other models are "
                      "unaffected", out.splitlines())
        _, _, err = self.go("run", "-a", "a")
        self.assertIn("a's Fable week is spent", err)
        _, _, err = self.go("run", "-a", "b")
        self.assertNotIn("Fable", err)

    def test_an_unspent_model_week_says_nothing(self):
        self.add("a")
        self.serve_usage("a")                   # Fable 27%
        self.assertNotIn("Fable", self.go("run")[2] + self.go("status")[1])

    def test_the_bar_shows_a_model_week_only_when_hot_by_its_model_name(self):
        acct = Account("a", "claude", load_plan("claude-pro"), self.tmp)
        t, reset = Tuning(), at(2026, 11, 12, 3)
        base = {"five_hour": Window(6, RECORDED + H), "seven_day": Window(40, reset)}
        cool = cli._status_line(acct, Snapshot(RECORDED, {**base, "seven_day:Fable": Window(27, reset)}),
                                RECORDED, True, t)
        self.assertEqual(cool, "a · 5h 6% · 7d 40%")
        hot = cli._status_line(acct, Snapshot(RECORDED, {**base, "seven_day:Fable": Window(96, reset)}),
                               RECORDED, True, t)
        self.assertEqual(hot, f"a · 5h 6% · 7d 40% · {cli.TIGHT}Fable 96% (Thu 03:00){cli.PLAIN}")
        spent = cli._status_line(acct, Snapshot(RECORDED, {**base, "seven_day:Fable": Window(99, reset)}),
                                 RECORDED, True, t)
        self.assertIn(f"{cli.SPENT}Fable 99% (Thu 03:00){cli.PLAIN}", spent)
        self.assertNotIn("seven_day:", spent)


class SignInEnds(Usage):
    def iso(self, t):
        return datetime.fromtimestamp(t, ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")

    def test_a_sign_in_ending_within_three_days_is_named(self):
        self.add("a", orgId=ORG + "a", loginExpiresAt=self.iso(RECORDED + 2 * 24 * H))
        self.add("b", orgId=ORG + "b", loginExpiresAt=self.iso(RECORDED + 4 * 24 * H))
        line = "a: sign-in ends Fri 10:50; `fairshare join a` signs it in again"
        _, _, err = self.go("run")
        self.assertIn(f"fairshare: {line}", err.splitlines())
        self.assertNotIn("b: sign-in ends", err)
        out = self.go("status")[1]
        self.assertIn(f"    {line}", out.splitlines())
        self.assertNotIn("b: sign-in ends", out)
        self.assertIn(line, self.go("run", "-a", "a")[2])
        self.assertNotIn("sign-in ends", self.go("run", "-a", "b")[2])

    def test_the_end_is_parsed_from_the_sign_in_check(self):
        for v in VERSIONS:
            with self.subTest(version=v.name):
                st = claude.parse_account_status(json.loads((v / "auth-status-pro.json").read_text()))
                self.assertEqual(st.expires_at, datetime(2030, 1, 2, 3, 4, 5, 678000,
                                                         ZoneInfo("UTC")).timestamp())
        for bad in (None, "", "soon", 5, "2030-01-02T03:04:05"):      # no zone: not guessed
            raw = {**json.loads((self.version / "auth-status-pro.json").read_text()),
                   "loginExpiresAt": bad}
            with self.subTest(value=bad):
                self.assertIsNone(claude.parse_account_status(raw).expires_at)

    def test_doctor_names_the_date(self):
        self.add("a")
        self.serve_usage("a")
        self.assertIn("pass  a: sign-in check answers: signed in until 2030-01-01, plan claude-pro",
                      self.go("doctor")[1].splitlines())        # 03:04 UTC: the day before here
        self.add("b", auth="logged-out")
        self.assertIn("pass  b: sign-in check answers: signed out", self.go("doctor")[1])


class Doctor(Usage):
    def test_each_kind_of_line(self):
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b", extra='usage_args = "-p /usage"\n')
        self.add("c", orgId=ORG + "c")
        self.serve_usage("a")
        self.serve_text("b", "Current session: 19% used · resets Nov 11 at 3:40pm (America/Denver)")
        code, out, _ = self.go("doctor")
        lines = out.splitlines()
        self.assertEqual(code, 1)
        self.assertIn("pass  a: usage probe answers (five_hour, seven_day, 1 model week), "
                      "local command", lines)
        self.assertIn("pass  b: usage probe answers (five_hour), with usage_args", lines)
        self.assertIn("FAIL  c: usage probe failed (no answer: exit 1)", lines)

    def test_doctor_records_nothing(self):
        self.add("a")
        self.serve_usage("a")
        self.go("doctor")
        self.assertIsNone(store.load("a"))


if __name__ == "__main__":
    unittest.main()
