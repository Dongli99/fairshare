import unittest
from pathlib import Path

from fairshare.model import Account, Snapshot, Tuning, Window, load_plan
from fairshare.policy import rank

H = 3600.0
NOW = 1_000_000.0
PRO, MAX, ULTRA = load_plan("claude-pro"), load_plan("claude-max"), load_plan("claude-ultra")


def acct(id, plan=PRO):
    return Account(id, "claude", plan, Path("/x"))


def snap(five=None, seven=None):
    w = {}
    if five:
        w["five_hour"] = Window(five[0], NOW + five[1] * H)
    if seven:
        w["seven_day"] = Window(seven[0], NOW + seven[1] * H)
    return Snapshot(NOW, w)


def order(accounts, snaps, t=Tuning()):
    return [v.account.id for v in rank(accounts, snaps, NOW, t)]


class WeeklyPace(unittest.TestCase):
    def test_account_resetting_soon_with_budget_left_wins(self):
        # b resets in 20h with 60% left; a resets in 150h with 60% left
        snaps = {"a": snap((10, 3), (40, 150)), "b": snap((10, 3), (40, 20))}
        self.assertEqual(order([acct("a"), acct("b")], snaps), ["b", "a"])

    def test_bigger_plan_outweighs_equal_percentage(self):
        snaps = {"p": snap((10, 3), (50, 80)), "m": snap((10, 3), (50, 80))}
        self.assertEqual(order([acct("p"), acct("m", MAX)], snaps), ["m", "p"])

    def test_ultra_without_a_five_hour_window_is_eligible_and_weighs_ten_pros(self):
        snaps = {"u": snap(None, (50, 80)), "p": snap((10, 3), (5, 80))}
        v = rank([acct("u", ULTRA), acct("p")], snaps, NOW)
        self.assertEqual([x.account.id for x in v], ["u", "p"])
        self.assertTrue(v[0].eligible)
        self.assertAlmostEqual(v[0].score, 10 * 50 / 80)

    def test_rolled_over_window_counts_as_fresh(self):
        stale = Snapshot(NOW, {"seven_day": Window(99, NOW - 10)})
        self.assertTrue(rank([acct("a")], {"a": stale}, NOW)[0].eligible)

    def test_no_snapshot_is_ranked_not_crashed(self):
        self.assertEqual(order([acct("a")], {}), ["a"])


class FiveHourGuard(unittest.TestCase):
    def test_hot_account_with_distant_reset_is_avoided(self):
        snaps = {"a": snap((95, 4), (10, 20)), "b": snap((10, 3), (20, 20))}
        self.assertEqual(order([acct("a"), acct("b")], snaps), ["b", "a"])

    def test_hot_account_resetting_within_an_hour_is_still_eligible(self):
        snaps = {"a": snap((95, 0.5), (10, 20)), "b": snap((10, 3), (80, 150))}
        self.assertEqual(order([acct("a"), acct("b")], snaps)[0], "a")

    def test_all_hot_picks_whichever_frees_first(self):
        snaps = {"a": snap((95, 4), (10, 20)), "b": snap((90, 2), (10, 20))}
        v = rank([acct("a"), acct("b")], snaps, NOW)
        self.assertEqual(v[0].account.id, "b")
        self.assertFalse(v[0].eligible)

    def test_exhausted_week_is_avoided(self):
        snaps = {"a": snap((10, 3), (99, 20)), "b": snap((10, 3), (80, 150))}
        self.assertEqual(order([acct("a"), acct("b")], snaps)[0], "b")

    def test_reset_exactly_at_the_grace_hour_counts_as_hot(self):
        v = rank([acct("a")], {"a": snap((95, 1.0), (10, 20))}, NOW)[0]
        self.assertFalse(v.eligible)


class OneRuleNotTwoTiers(unittest.TestCase):
    def test_huge_expiring_budget_beats_a_nearly_empty_account_despite_hot_window(self):
        # A: Max (5x), week untouched, resets in 3h, 5h window 90%. B: Pro, week 97% used.
        snaps = {"A": snap((90, 1.5), (0, 3)), "B": snap((5, 3), (97, 160))}
        self.assertEqual(order([acct("A", MAX), acct("B")], snaps), ["A", "B"])

    def test_blocked_week_is_not_rescued_by_a_short_window_reset(self):
        # exh: week 99% used for 100h (5h window nearly resets). hot: 5h window full, week free.
        snaps = {"exh": snap((10, 0.2), (99, 100)), "hot": snap((95, 2), (10, 20))}
        self.assertEqual(order([acct("exh"), acct("hot")], snaps), ["hot", "exh"])

    def test_two_exhausted_weeks_pick_the_one_that_frees_first(self):
        snaps = {"far": snap(None, (99, 100)), "near": snap(None, (99, 3))}
        self.assertEqual(order([acct("far"), acct("near")], snaps), ["near", "far"])


    def test_nearly_spent_week_does_not_outrank_a_healthy_account_just_by_resetting_soon(self):
        snaps = {"full": snap((0, 4), (99.5, 5)), "ok": snap((0, 4), (95, 160))}
        self.assertEqual(order([acct("full"), acct("ok")], snaps), ["ok", "full"])

    def test_exhausted_threshold_is_inclusive(self):
        snaps = {"edge": snap((0, 4), (98.0, 5)), "ok": snap((0, 4), (97.0, 160))}
        self.assertEqual(order([acct("edge"), acct("ok")], snaps), ["ok", "edge"])


class Reasons(unittest.TestCase):
    def test_missing_weekly_window_is_reported_as_assumed(self):
        for sn in (None, Snapshot(NOW, {}), snap((10, 3))):
            v = rank([acct("a")], {} if sn is None else {"a": sn}, NOW)[0]
            self.assertIn("assumed fresh", v.reason)


class UsableIn(unittest.TestCase):
    """Hours until an account is eligible again, for the all-spent prompt."""
    def usable_in(self, sn):
        return rank([acct("a")], {"a": sn}, NOW)[0].usable_in

    def test_eligible_now_is_zero(self):
        self.assertEqual(self.usable_in(snap((50, 3), (50, 20))), 0.0)
        self.assertEqual(self.usable_in(snap((95, 0.5), (10, 20))), 0.0)   # within grace

    def test_hot_waits_for_the_short_window(self):
        self.assertAlmostEqual(self.usable_in(snap((95, 4), (10, 20))), 4.0)

    def test_spent_week_waits_for_the_week_even_if_the_short_window_resets_first(self):
        self.assertAlmostEqual(self.usable_in(snap((95, 4), (99, 30))), 30.0)
        self.assertAlmostEqual(self.usable_in(snap((10, 4), (99, 30))), 30.0)


class Capacity(unittest.TestCase):
    def test_zero_or_negative_capacity_is_rejected_not_defaulted(self):
        for bad in (0, 0.0, -1):
            with self.assertRaises(ValueError):
                load_plan("claude-pro", bad)


if __name__ == "__main__":
    unittest.main()
