import unittest
from pathlib import Path

from fairshare.model import Account, Snapshot, Tuning, Window, load_plan
from fairshare.policy import rank, spread, versus

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

    def test_hot_account_resetting_within_an_hour_is_eligible_when_no_other_is(self):
        snaps = {"a": snap((95, 0.5), (10, 20)), "b": snap((95, 3), (80, 150))}
        v = rank([acct("a"), acct("b")], snaps, NOW)
        self.assertEqual((v[0].account.id, v[0].eligible), ("a", True))

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


class GraceIsALastResort(unittest.TestCase):
    """The grace hour holds only while no account is eligible without it (decision 26)."""

    def test_an_account_that_can_work_takes_the_launch(self):
        # Seen in a simulation: a's 5h 96% with 30 minutes to go would have taken a session
        # that stops at its limit in minutes, while b sat at 12%.
        snaps = {"a": snap((96, 0.5), (63.5, 43.5)), "b": snap((12, 4.5), (51.5, 91.5))}
        a, b = sorted(rank([acct("a"), acct("b")], snaps, NOW), key=lambda v: v.account.id)
        self.assertFalse(a.eligible)
        self.assertTrue(b.eligible)
        self.assertAlmostEqual(a.usable_in, 0.5)                   # at its reset
        self.assertIn("five_hour 96% used, resets in 30m", a.reason)

    def test_a_hot_account_outside_the_grace_keeps_its_usable_in(self):
        snaps = {"a": snap((95, 4), (10, 20)), "b": snap((10, 3), (20, 20))}
        a = next(v for v in rank([acct("a"), acct("b")], snaps, NOW) if v.account.id == "a")
        self.assertAlmostEqual(a.usable_in, 3.0)                   # reset minus the grace hour

    def test_a_spent_or_hot_other_leaves_the_grace_in_place(self):
        for other in (snap((99, 3), (10, 20)), snap((10, 3), (99, 20)), snap((90, 3), (10, 20))):
            with self.subTest(other=other):
                v = rank([acct("a"), acct("b")],
                         {"a": snap((95, 0.5), (10, 20)), "b": other}, NOW)
                self.assertEqual([x.account.id for x in v if x.eligible], ["a"])


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

    def test_hot_waits_until_the_short_reset_is_within_grace(self):
        # resets in 4h, grace 1h: eligible again after 3h, not 4h
        self.assertAlmostEqual(self.usable_in(snap((95, 4), (10, 20))), 3.0)

    def test_usable_in_agrees_with_eligibility(self):
        sn = snap((95, 4), (10, 20))
        u = self.usable_in(sn)
        before = rank([acct("a")], {"a": sn}, NOW + (u - 0.01) * H)[0]
        after = rank([acct("a")], {"a": sn}, NOW + (u + 0.01) * H)[0]
        self.assertFalse(before.eligible)
        self.assertTrue(after.eligible)

    def test_spent_week_waits_for_the_week_even_if_the_short_window_resets_first(self):
        self.assertAlmostEqual(self.usable_in(snap((95, 4), (99, 30))), 30.0)
        self.assertAlmostEqual(self.usable_in(snap((10, 4), (99, 30))), 30.0)


class ReasonDurations(unittest.TestCase):
    def test_under_an_hour_reads_in_minutes(self):
        v = rank([acct("a")], {"a": snap((10, 3), (50, 0.15))}, NOW)[0]
        self.assertIn("resets in 9m", v.reason)

    def test_hours_stay_hours(self):
        self.assertIn("resets in 20h", rank([acct("a")], {"a": snap((10, 3), (50, 20))}, NOW)[0].reason)
        self.assertIn("resets in 4.0h", rank([acct("a")], {"a": snap((95, 4), (10, 20))}, NOW)[0].reason)


class Capacity(unittest.TestCase):
    def test_zero_or_negative_capacity_is_rejected_not_defaulted(self):
        for bad in (0, 0.0, -1):
            with self.assertRaises(ValueError):
                load_plan("claude-pro", bad)


if __name__ == "__main__":
    unittest.main()


class Spread(unittest.TestCase):
    """Sessions started close together do not all land on one account."""
    CLOSE = 0.10

    def ids(self, snaps, running, accounts=None):
        vs = rank(accounts or [acct(i) for i in snaps], snaps, NOW)
        return [v.account.id for v in spread(vs, running, self.CLOSE)]

    def test_within_close_the_account_with_fewer_sessions_goes_first(self):
        snaps = {"a": snap((10, 3), (40, 72)), "b": snap((10, 3), (42, 72))}   # 3% apart
        self.assertEqual(self.ids(snaps, {}), ["a", "b"])
        self.assertEqual(self.ids(snaps, {"a": 1}), ["b", "a"])
        self.assertEqual(self.ids(snaps, {"a": 1, "b": 1}), ["a", "b"])      # a tie: the ranking

    def test_outside_close_the_ranking_stands(self):
        snaps = {"a": snap((10, 3), (50, 48)), "b": snap((10, 3), (50, 96))}   # 2x apart
        self.assertEqual(self.ids(snaps, {"a": 3}), ["a", "b"])

    def test_close_is_measured_from_the_best_eligible_and_never_picks_an_ineligible_one(self):
        # hot: outscores both but cannot take work; a and b are close
        snaps = {"hot": snap((90, 3), (50, 2)), "a": snap((10, 3), (40, 72)),
                 "b": snap((10, 3), (42, 72))}
        self.assertEqual(self.ids(snaps, {"a": 2, "hot": 0}), ["hot", "b", "a"])

    def test_nothing_eligible_is_left_alone(self):
        snaps = {"a": snap((10, 3), (99, 5)), "b": snap((10, 3), (99, 9))}
        self.assertEqual(self.ids(snaps, {"a": 4}), ["a", "b"])


class PlannedReset(unittest.TestCase):
    """A full reset the user plans this week: its refill counts as room in the week."""

    def test_the_planned_refill_counts_as_room_left(self):
        snaps = {"a": snap((10, 3), (50, 80)), "b": snap((10, 3), (20, 80))}
        self.assertEqual(order([acct("a"), acct("b")], snaps), ["b", "a"])
        v = rank([acct("a"), acct("b")], snaps, NOW, extra={"a": 100.0})
        self.assertEqual([x.account.id for x in v], ["a", "b"])
        self.assertAlmostEqual(v[0].score, 150 / 80)
        self.assertIn("+100% planned (50% left until it is used)", v[0].reason)

    def test_a_spent_week_stays_spent_until_the_refill_shows(self):
        snaps = {"a": snap((10, 3), (100, 80)), "b": snap((10, 3), (90, 80))}
        v = rank([acct("a"), acct("b")], snaps, NOW, extra={"a": 100.0})
        self.assertEqual([(x.account.id, x.eligible) for x in v], [("b", True), ("a", False)])

    def test_no_plan_ranks_as_before(self):
        snaps = {"a": snap((10, 3), (50, 80))}
        self.assertEqual(rank([acct("a")], snaps, NOW, extra={}),
                         rank([acct("a")], snaps, NOW))


class Versus(unittest.TestCase):
    """Why the first account goes before the next: the rule and both reasons, since percentages
    alone point the other way when the room expires sooner or a refill is planned."""

    def test_the_rule_and_both_reasons(self):
        snaps = {"a": snap((10, 3), (74, 107)), "b": snap((10, 3), (43, 137))}
        v = rank([acct("a"), acct("b")], snaps, NOW, extra={"a": 100.0})
        self.assertEqual(versus(v[0], v), (
            "a before b (score 1.18 vs 0.42: room left per hour until it resets, so room that "
            "would expire unused is spent first); a: seven_day 74% used, resets in 107h, +100% "
            "planned (26% left until it is used); b: seven_day 43% used, resets in 137h"))

    def test_a_lower_score_put_first_says_why(self):
        snaps = {"a": snap((10, 3), (40, 72)), "b": snap((10, 3), (42, 72))}
        v = spread(rank([acct("a"), acct("b")], snaps, NOW), {"a": 1}, 0.10)
        self.assertIn("b before a (score 0.81 vs 0.83, close enough that the one with fewer "
                      "sessions running, or the preferred one, goes first)", versus(v[0], v))

    def test_skips_an_account_that_cannot_take_work_and_is_empty_without_one(self):
        snaps = {"hot": snap((90, 3), (50, 2)), "a": snap((10, 3), (40, 72)),
                 "b": snap((10, 3), (99, 72))}
        v = rank([acct(i) for i in snaps], snaps, NOW)
        a = next(x for x in v if x.account.id == "a")
        self.assertEqual(versus(a, v), "")
