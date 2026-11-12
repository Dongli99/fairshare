"""Stage 5c, part B: a session stopped by a limit that resets soon is left for Claude Code's own
resume. Same fleets as test_handover and test_watch (temp HOME, config, state and hub; fakes
first on PATH, proved in Fleet.setUp; inherited CLAUDE*, FAIRSHARE_* and BROWSER cleared)."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.providers import claude
from fairshare.model import ConfigError, Request, SessionSeen, load_accounts

from test_handover import Handover
from test_honest import H, NOW
from test_pickup import SID


class Setting(unittest.TestCase):
    def tuning(self, policy):
        with mock.patch("pathlib.Path.read_text", return_value=f'hub = "/h"\n[policy]\n{policy}'):
            return load_accounts(Path("/nowhere/accounts.toml"))[1]

    def test_default_zero_and_refusal(self):
        self.assertEqual(self.tuning("").resume_wait_minutes, 30)
        self.assertEqual(self.tuning("resume_wait_minutes = 0").resume_wait_minutes, 0)
        self.assertEqual(self.tuning("resume_wait_minutes = 45").resume_wait_minutes, 45)
        for bad in ("-1", "'x'"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as e:
                self.tuning(f"resume_wait_minutes = {bad}")
            self.assertIn("resume_wait_minutes", str(e.exception))


class LauncherWaits(Handover):
    """a's 5h window is spent; when it resets is set per test (Stranded: in three hours)."""

    def policy(self, text):
        cfg = self.tmp / "accounts.toml"
        cfg.write_text(cfg.read_text().replace("\n\n[[account]]", f"\n[policy]\n{text}\n\n"
                                                                    "[[account]]", 1))

    def out_until(self, hours):
        self.snap("a", five=(100, hours), seven=(50, 90))

    def test_a_reset_within_the_wait_leaves_the_child_and_prints_nothing(self):
        self.record()
        self.out_until(0.4)                                          # 24 minutes
        self.launches("run", "-a", "a")                              # links made: said once
        _, before = self.launches("run", "-a", "a")
        self.record()
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual((code, len(self.calls), err), (0, 1, before))   # no pickup, no line
        rec = store.session(SID)
        self.assertEqual((rec.account, rec.waiting_until), ("a", NOW + 0.4 * H))
        self.assertIsNotNone(rec.stranded_at)
        self.assertEqual(json.loads((self.tmp / "state/sessions" / f"{SID}.json").read_text())
                         ["waiting_until"], NOW + 0.4 * H)

    def test_a_week_and_a_short_window_both_spent_wait_for_the_later(self):
        self.record()
        self.snap("a", five=(100, 0.2), seven=(99, 0.4))
        self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(store.session(SID).waiting_until, NOW + 0.4 * H)

    def test_beyond_the_wait_it_picks_up_as_today(self):
        self.record()
        self.out_until(0.6)                                          # 36 minutes
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])
        self.assertIsNone(store.session(SID).waiting_until)

    def test_zero_always_picks_up(self):
        self.policy("resume_wait_minutes = 0")
        self.record()
        self.out_until(0.1)
        self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])

    def test_a_tool_that_does_not_resume_by_itself_is_always_picked_up(self):
        self.record()
        self.out_until(0.1)
        with mock.patch.object(claude, "resumes_at_reset", False):
            self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])

    def test_a_longer_setting_waits_longer(self):
        self.policy("resume_wait_minutes = 60")
        self.record()
        self.out_until(0.9)
        self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(len(self.calls), 1)

    def test_a_manual_pickup_always_moves(self):
        self.record()
        self.out_until(0.2)
        store.update_session(SID, lambda old: old.__class__(
            **{**vars(old), "waiting_until": NOW + 0.2 * H}))
        self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)])
        self.assertEqual(self.started_args(), (["--resume", SID, "fairshare moved this session "
                                                "from account a to account b because a hit its "
                                                "usage limit. Continue where you left off."], "b"))
        self.assertIsNone(store.session(SID).waiting_until)          # moved: no longer waiting
        self.out_until(0.2)
        self.record()
        code, out, err = self.go("pickup", SID, "--bg")              # and from the command
        self.assertEqual(code, 0, err)
        self.assertEqual(store.session(SID).account, "b")


class Bar(Handover):
    def capture(self):
        raw = json.dumps({"session_id": SID, "cwd": str(self.work)})
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw.encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            return self.go("capture")[1]

    def wait_until(self, t):
        store.update_session(SID, lambda old: old.__class__(**{**vars(old), "waiting_until": t}))

    def test_waiting_and_not_waiting(self):
        self.record()
        self.snap("a", five=(100, 0.4), seven=(50, 90))
        self.assertTrue(self.capture().endswith(" · \033[31mout · ! fairshare pickup\033[0m\n"))
        self.wait_until(NOW + 0.4 * H)
        self.assertTrue(self.capture().endswith(
            " · \033[31mout · resumes 08:24 · ! fairshare pickup to move now\033[0m\n"))
        self.clock[0] += 0.5 * H                                     # past it: today's mark
        self.snap("a", five=(100, 3), seven=(50, 90), at=NOW - 60)
        self.assertIn("out · ! fairshare pickup\033[0m", self.capture())

    def test_cleared_with_the_stranded_mark(self):
        self.record()
        self.wait_until(NOW + 0.4 * H)
        self.snap("a", five=(3, 5), seven=(50, 90), at=NOW)          # newer numbers: it reset
        self.assertNotIn("out", self.capture())
        rec = store.session(SID)
        self.assertEqual((rec.stranded_at, rec.waiting_until), (None, None))

    def test_a_new_stop_ends_an_old_wait(self):
        self.record()
        self.wait_until(NOW + 0.4 * H)
        self.hook({})
        self.assertIsNone(store.session(SID).waiting_until)

