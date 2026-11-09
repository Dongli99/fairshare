import unittest
from pathlib import Path

from fairshare.model import Account, Snapshot, Tuning, Window, load_plan
from fairshare.policy import rank

H = 3600.0
NOW = 1_000_000.0
PRO, MAX20 = load_plan("claude-pro"), load_plan("claude-max20x")


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
        self.assertEqual(order([acct("p"), acct("m", MAX20)], snaps), ["m", "p"])

    def test_rolled_over_window_counts_as_fresh(self):
        stale = Snapshot(NOW, {"seven_day": Window(99, NOW - 10)})
        self.assertTrue(rank([acct("a")], {"a": stale}, NOW)[0].eligible)

    def test_no_snapshot_is_ranked_not_crashed(self):
        self.assertEqual(order([acct("a")], {}), ["a"])


class FiveHourGuard(unittest.TestCase):
    def test_hot_account_with_distant_reset_is_avoided(self):
        snaps = {"a": snap((95, 4), (10, 20)), "b": snap((10, 3), (80, 150))}
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


if __name__ == "__main__":
    unittest.main()
