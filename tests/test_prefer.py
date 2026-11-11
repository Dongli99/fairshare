"""`fairshare prefer`: a timed preference for one account. It wins while that account can take
work, gives way (with a notice) when it cannot, and ends on its own."""
import unittest

from fairshare import cli, policy, store
from fairshare.model import Account, Snapshot, Window, load_plan

from test_honest import H, NOW, ORG, Fleet


class Prefer(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", seven=(10, 20))       # a ranks first by far
        self.snap("b", seven=(50, 100))

    def dir(self, id):
        return str(self.tmp / "hub/accounts" / id)

    def test_the_preferred_account_is_launched_and_status_says_so(self):
        self.assertEqual(self.go("prefer", "b", "--for", "3h")[0], 0)
        code, out, _ = self.go("status")
        first = out.splitlines()[0]                                          # NOW is 08:00 UTC
        self.assertTrue(first.startswith("> b ") and first.endswith("  [preferred until 11:00]"), out)
        self.go("run")
        self.assertEqual(self.launched_dir(), self.dir("b"))

    def test_an_account_that_cannot_take_work_gives_way_with_a_notice(self):
        self.snap("b", seven=(99, 100))
        self.go("prefer", "b")
        code, _, err = self.go("run")
        self.assertEqual(self.launched_dir(), self.dir("a"))
        self.assertIn("preferring b until", err)
        self.assertIn("cannot take work now, so the best account is used", err)
        out = self.go("status")[1]
        self.assertTrue(out.startswith("preferring b until Sat 00:00; it cannot take work"), out)
        self.assertNotIn("[preferred", out)

    def test_it_ends_on_its_own_and_by_default_at_the_end_of_today(self):
        self.go("prefer", "b")
        self.assertEqual(store.preference(NOW), ("b", NOW + 16 * H))     # Sat 00:00 UTC
        self.clock[0] = cli._midnight(NOW) + 1
        self.go("run")
        self.assertEqual(self.launched_dir(), self.dir("a"))
        self.assertEqual(self.go("prefer")[1], "no preferred account\n")

    def test_off_ends_it_and_run_a_is_untouched(self):
        self.go("prefer", "b", "--for", "2d")
        self.go("prefer", "--off")
        self.assertIsNone(store.preference(NOW))
        self.go("prefer", "b")
        self.go("run", "-a", "a")
        self.assertEqual(self.launched_dir(), self.dir("a"))

    def test_an_unknown_account_or_length_is_refused_and_nothing_is_stored(self):
        self.assertEqual(self.go("prefer", "nope")[0], 2)
        for bad in ("3", "0h", "3w", "-1h"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.go("prefer", "b", "--for", bad)
        self.assertIsNone(store.preference(NOW))


class PurePrefer(unittest.TestCase):
    def test_the_preferred_eligible_verdict_moves_first_and_nothing_else_changes(self):
        plan = load_plan("claude-pro")
        accts = [Account(i, "claude", plan, None) for i in "xyz"]
        snaps = {"x": Snapshot(NOW, {"seven_day": Window(10, NOW + 20 * H)}),
                 "y": Snapshot(NOW, {"seven_day": Window(99, NOW + 20 * H)}),
                 "z": Snapshot(NOW, {"seven_day": Window(50, NOW + 90 * H)})}
        ranked = policy.rank(accts, snaps, NOW)
        ids = lambda vs: [v.account.id for v in vs]
        self.assertEqual(ids(policy.prefer(ranked, "z")), ["z", "x", "y"])
        self.assertEqual(policy.prefer(ranked, "y"), ranked)       # spent: ignored
        self.assertEqual(policy.prefer(ranked, "gone"), ranked)


if __name__ == "__main__":
    unittest.main()
