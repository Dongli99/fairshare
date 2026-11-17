"""At a limit, wait with a message; never resume on an account that is out (seen live, 2026-11-17:
a's 5h window spent with the reset 40 minutes away, a's week the best, and the session restarted
on a, limit, and round again). Same fleets as test_wait (temp HOME, config, state and hub; fakes
first on PATH, proved in Fleet.setUp; inherited CLAUDE*, FAIRSHARE_* and BROWSER cleared)."""
import io
import os
import unittest
from unittest import mock

from fairshare import cli, session, store
from fairshare.model import Request, SessionSeen, Tuning
from fairshare.policy import rank

from test_handover import Handover
from test_honest import H, NOW
from test_pickup import SID
from test_policy import NOW as T0, acct, order, snap

FORTY = 40 / 60          # hours: the live reset


class SpentShortWindow(unittest.TestCase):
    """The policy: a spent short window is a hard rule, a nearly full one a preference."""

    def test_spent_and_resetting_in_40_minutes_ranks_below_a_worse_week(self):
        # a: week barely used and resetting soon (the better weekly numbers), 5h spent.
        snaps = {"a": snap((100, FORTY), (10, 20)), "b": snap((10, 3), (80, 150))}
        vs = rank([acct("a"), acct("b")], snaps, T0)
        self.assertEqual([v.account.id for v in vs], ["b", "a"])
        a = vs[1]
        self.assertFalse(a.eligible)
        self.assertAlmostEqual(a.score, -FORTY)                     # with the unusable ones
        self.assertAlmostEqual(a.usable_in, FORTY)                  # at its reset, no grace
        self.assertIn("five_hour 100% used, resets in 40m", a.reason)

    def test_the_line_is_the_weeks(self):
        t = Tuning()
        for used, eligible in ((t.long_exhausted_pct, False), (t.long_exhausted_pct - 1, True)):
            with self.subTest(used=used):
                v = rank([acct("a")], {"a": snap((used, FORTY), (10, 20))}, T0)[0]
                self.assertEqual(v.eligible, eligible)

    def test_nearly_full_and_resetting_in_40_minutes_is_used_only_when_nothing_else_works(self):
        # b can work now: a's grace does not hold (what is left of its window lasts minutes)
        snaps = {"a": snap((90, FORTY), (10, 20)), "b": snap((10, 3), (80, 150))}
        a, b = rank([acct("a"), acct("b")], snaps, T0)
        self.assertEqual((a.account.id, a.eligible, b.eligible), ("a", False, True))
        self.assertAlmostEqual(a.score, 90 / 20 * 0.10)            # scaled like any hot window
        self.assertAlmostEqual(a.usable_in, FORTY)
        # alone, or with nothing else able to work, the grace holds
        for others in ([], [("b", snap((95, 3), (10, 20)))]):
            with self.subTest(others=others):
                vs = rank([acct("a")] + [acct(i) for i, _ in others],
                          {"a": snaps["a"], **dict(others)}, T0)
                self.assertEqual((vs[0].account.id, vs[0].eligible), ("a", True))
                self.assertAlmostEqual(vs[0].score, 90 / 20)       # not scaled: inside the grace
                self.assertEqual(vs[0].usable_in, 0.0)

    def test_a_spent_week_and_a_spent_short_window_wait_for_the_later(self):
        v = rank([acct("a")], {"a": snap((100, 4), (99, 2))}, T0)[0]
        self.assertAlmostEqual(v.score, -4)
        self.assertAlmostEqual(v.usable_in, 4)
        self.assertEqual(order([acct("a"), acct("b")],
                               {"a": snap((100, 4), (99, 2)), "b": snap((100, 3), None)}),
                         ["b", "a"])


class Live(Handover):
    """The live scenario: a's 5h window spent, resetting in 40 minutes; a's week the best of
    all; b the best account that can take work (c a little behind)."""

    def setUp(self):
        super().setUp()
        self.snap("a", five=(100, FORTY), seven=(10, 20))
        self.snap("b", five=(10, 2), seven=(80, 150))
        self.snap("c", five=(10, 2), seven=(90, 150))

    def accounts_of(self):
        return [self.started_args(n)[1] for n in range(len(self.calls))]


class LauncherAtALimit(Live):
    def test_by_default_it_waits_with_the_message_and_never_restarts_on_a(self):
        self.record()
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual((code, self.accounts_of()), (0, ["a"]))     # the child runs on
        self.assertIn("fairshare: a is out (5h limit) · Claude Code resumes this session at 08:40 "
                      "· ! fairshare pickup moves it to b now\n", err)
        rec = store.session(SID)
        self.assertEqual((rec.account, rec.waiting_until, rec.limits),
                         ("a", NOW + FORTY * H, {"a": NOW + FORTY * H}))
        self.assertIsNotNone(rec.stranded_at)

    def test_opted_in_a_reset_40_minutes_away_moves_to_the_best_usable_account(self):
        self.policy("resume_wait_minutes = 30")
        self.record()
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual((code, self.accounts_of()), (0, ["a", "b"]), err)
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])
        self.assertIn("fairshare moved this session from account a to account b",
                      self.started_args()[0][2])

    def test_with_every_account_out_it_waits_for_the_reset_and_says_so(self):
        self.policy("resume_wait_minutes = 30")                     # even opted in
        self.snap("b", five=(100, 2), seven=(80, 150))
        self.snap("c", five=(100, 0.5), seven=(90, 150))
        self.record()
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual((code, self.accounts_of()), (0, ["a"]))
        self.assertIn("fairshare: a is out (5h limit) · Claude Code resumes this session at 08:40 "
                      "· no account can take work now (the soonest: c, usable at 08:30)\n", err)
        self.assertEqual(store.session(SID).waiting_until, NOW + FORTY * H)


class Guard(Live):
    """No loop from a wrong ranking: the launcher's own record of the limit decides."""

    def forced_to_a(self):
        """`_route` made to launch on a, whatever the policy says."""
        a = next(v for v in cli._survey().verdicts if v.account.id == "a")
        return mock.patch.object(cli, "_route", lambda args, **kw: cli._launch(
            a, args, continues=kw.get("continues")))

    def test_a_forced_wrong_ranking_never_resumes_on_a_before_its_reset(self):
        self.policy("resume_wait_minutes = 0")                      # always move
        self.record()
        with self.forced_to_a():
            code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual((code, self.accounts_of()), (75, ["a"]))    # not resumed on a
        self.assertIn(f"fairshare: a's limit stopped this session and lifts at 08:40; not "
                      f"resuming it there before then. To resume it later: fairshare run -a a "
                      f"-- --resume {SID}\n", err)

    def test_a_manual_pickup_forced_to_a_is_refused_too_and_allowed_after_the_reset(self):
        self.record()
        self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])   # waits, holds a
        with self.forced_to_a():
            code, _ = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)])
        self.assertEqual((code, self.accounts_of()), (75, ["a"]))
        self.clock[0] = NOW + FORTY * H + 60                         # the limit lifted
        self.snap("a", five=(0, 5), seven=(10, 20), at=self.clock[0])
        with self.forced_to_a():
            self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)])
        self.assertEqual(self.accounts_of(), ["a", "a"])

    def test_two_strandings_in_a_row_stop_with_the_resume_line(self):
        self.policy("resume_wait_minutes = 0")
        self.record()
        self.calls = []

        def run_child(cmd, env, on_request=None):                   # each start stopped on a
            self.calls.append((cmd, env, os.getcwd()))
            self.record()                                            # however it got there
            store.save_request(os.getpid(), Request(SID, False, 4321))
            return session.PICKUP if on_request(4321) else 0
        err = io.StringIO()
        with mock.patch.object(session, "run_child", run_child), \
                mock.patch.object(cli, "_interactive", return_value=False), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            code = cli.main(["run", "-a", "a"])
        err = err.getvalue()
        self.assertEqual((code, self.accounts_of()), (75, ["a", "b"]))   # no third start
        self.assertIn(f"fairshare: a stopped this session again before its limit lifted (5h "
                      f"resets 08:40); not trying again. To resume it later: fairshare run -a a "
                      f"-- --resume {SID}\n", err)

    def test_a_limit_survives_a_move_under_a_new_id(self):
        self.record()
        cli._hold(SID, "a", NOW + FORTY * H, NOW)
        new = "0a1b2c3d-0000-4000-8000-000000000002"
        store.link_session(SID, SessionSeen(new, str(self.work)), "b", NOW)
        self.assertEqual(store.session(new).limits, {"a": NOW + FORTY * H})


class ManualPickup(Live):
    def test_with_a_spent_it_picks_b(self):
        self.record()
        code, out, err = self.inside("pickup")                       # no launcher: background
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[:5], ["--bg", "--name", "pickup-0a1b2c3d", "--resume",
                                                 SID])
        self.assertEqual(self.bg_args("a"), [])

    def test_with_a_spent_a_launcher_resumes_on_b(self):
        self.record()
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)])
        self.assertEqual((code, self.accounts_of()), (0, ["a", "b"]), err)

    def test_with_all_spent_it_says_so_and_starts_nothing(self):
        self.snap("b", five=(100, 2), seven=(80, 150))
        self.snap("c", five=(100, 0.5), seven=(90, 150))
        self.record()
        line = ("fairshare: no account can take work now; the soonest is c, usable at 08:30; "
                "nothing done\n")
        launcher = self.started()
        code, out, err = self.inside("pickup", launcher=launcher)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.endswith(line), err)
        self.assertIsNone(store.take_request(launcher.pid))          # the launcher not asked
        self.assertIsNone(launcher.poll())                           # nor signalled
        code, _, err = self.inside("pickup")                         # in the background
        self.assertEqual((code, self.bg_args("a"), self.bg_args("b"), self.bg_args("c")),
                         (1, [], [], []))
        self.assertTrue(err.endswith(line), err)
        code, _, err = self.go("pickup", SID, tty=True)              # from a terminal
        self.assertEqual(code, 1)
        self.launched.assert_not_called()
        self.assertTrue(err.endswith(line), err)


if __name__ == "__main__":
    unittest.main()
