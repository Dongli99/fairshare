"""Written by a separate reviewing agent from the intent + model.py only, before it read policy.py.
Adopted into the suite; two of its original cases were dropped after review (wrong expectations).
`rank()` below is the single adapter to the unknown policy API; bound in step 2.
Expectations come from the intent, not from the implementation.
"""
import unittest
from pathlib import Path

from fairshare.model import Account, Plan, Snapshot, Tuning, Window, load_plan

H = 3600.0
NOW = 1_000_000.0
SHORT, LONG = "five_hour", "seven_day"


def plan(cap=1.0):
    return Plan(f"p{cap}", "claude", cap, LONG, 168.0, SHORT)


def acct(i, cap=1.0):
    return Account(i, "claude", plan(cap), Path("/x"))


def snap(short=None, long=None, at=NOW):
    """short/long = (used_pct, hours_until_reset) or None for missing window."""
    w = {}
    if short is not None:
        w[SHORT] = Window(short[0], NOW + short[1] * H)
    if long is not None:
        w[LONG] = Window(long[0], NOW + long[1] * H)
    return Snapshot(at, w)


def rank(entries, now=NOW, tuning=None):
    """entries: list of (Account, Snapshot|None). Return list of account ids, best first."""
    from fairshare.policy import rank as _rank
    accounts = [a for a, _ in entries]
    snaps = {a.id: s for a, s in entries if s is not None}
    return [v.account.id for v in _rank(accounts, snaps, now, tuning or Tuning())]


class Weekly(unittest.TestCase):
    def test_sooner_reset_preferred_at_equal_usage(self):
        r = rank([(acct("far"), snap((10, 4), (50, 120))),
                  (acct("near"), snap((10, 4), (50, 12)))])
        self.assertEqual(r[0], "near")

    def test_less_used_preferred_at_equal_reset(self):
        r = rank([(acct("busy"), snap((10, 4), (80, 48))),
                  (acct("idle"), snap((10, 4), (20, 48)))])
        self.assertEqual(r[0], "idle")

    def test_plan_size_not_percent(self):
        # same % and reset: the 20x has 20x the absolute budget left
        r = rank([(acct("small", 1), snap((10, 4), (50, 48))),
                  (acct("big", 20), snap((10, 4), (50, 48)))])
        self.assertEqual(r[0], "big")

    def test_big_plan_slightly_more_used_still_more_budget(self):
        # big: 20 * 40% left = 8 units; small: 1 * 90% left = 0.9 units
        r = rank([(acct("small", 1), snap((10, 4), (10, 48))),
                  (acct("big", 20), snap((10, 4), (60, 48)))])
        self.assertEqual(r[0], "big")

    def test_capacity_override_changes_ranking(self):
        a = Account("a", "claude", load_plan("claude-pro", 20.0), Path("/x"))
        b = Account("b", "claude", load_plan("claude-pro"), Path("/x"))
        r = rank([(b, snap((10, 4), (50, 48))), (a, snap((10, 4), (50, 48)))])
        self.assertEqual(r[0], "a")

    def test_capacity_override_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            load_plan("claude-pro", 0.0)

    def test_near_reset_with_little_left_not_over_preferred(self):
        # 1 s from reset, 1% left (tiny absolute value) vs healthy account with lots left
        r = rank([(acct("tiny"), snap((0, 4), (99, 1 / H))),
                  (acct("healthy"), snap((0, 4), (10, 100)))])
        # either order defensible; the point is: no crash, finite. Record result.
        self.assertEqual(set(r), {"tiny", "healthy"})

    def test_near_reset_with_lots_left_preferred(self):
        r = rank([(acct("expiring"), snap((0, 4), (10, 2))),
                  (acct("fresh"), snap((0, 4), (10, 160)))])
        self.assertEqual(r[0], "expiring")

    def test_reset_one_second_away_finite(self):
        r = rank([(acct("a"), snap((0, 4), (0, 1 / H))),
                  (acct("b"), snap((0, 4), (0, 100)))])
        self.assertEqual(len(r), 2)

    def test_three_accounts_ordering(self):
        r = rank([(acct("c"), snap((0, 4), (90, 100))),
                  (acct("a"), snap((0, 4), (10, 10))),
                  (acct("b"), snap((0, 4), (50, 50)))])
        self.assertEqual(r, ["a", "b", "c"])

    def test_order_independent_of_input_order(self):
        e = [(acct("a"), snap((0, 4), (10, 10))),
             (acct("b"), snap((0, 4), (50, 50))),
             (acct("c"), snap((0, 4), (90, 100)))]
        self.assertEqual(rank(e), rank(list(reversed(e))))

    def test_tie_is_deterministic_and_complete(self):
        e = [(acct("a"), snap((0, 4), (50, 50))), (acct("b"), snap((0, 4), (50, 50)))]
        self.assertEqual(rank(e), rank(list(reversed(e))))


class Exhausted(unittest.TestCase):
    def test_exhausted_long_ranks_last(self):
        r = rank([(acct("full"), snap((0, 4), (99.5, 5))),
                  (acct("ok"), snap((0, 4), (95, 160)))])
        self.assertEqual(r[0], "ok")

    def test_exactly_threshold_98(self):
        r = rank([(acct("edge"), snap((0, 4), (98.0, 5))),
                  (acct("ok"), snap((0, 4), (97.0, 160)))])
        self.assertEqual(r[0], "ok")

    def test_all_exhausted_still_returns_all(self):
        r = rank([(acct("a"), snap((0, 4), (100, 5))),
                  (acct("b"), snap((0, 4), (100, 50)))])
        self.assertEqual(set(r), {"a", "b"})

    def test_all_exhausted_prefers_soonest_reset(self):
        r = rank([(acct("far"), snap((0, 4), (100, 100))),
                  (acct("near"), snap((0, 4), (100, 2)))])
        self.assertEqual(r[0], "near")

    def test_over_100_pct_does_not_go_negative_and_invert(self):
        r = rank([(acct("over"), snap((0, 4), (130, 2))),
                  (acct("ok"), snap((0, 4), (90, 100)))])
        self.assertEqual(r[0], "ok")


class Short(unittest.TestCase):
    def test_hot_short_avoided(self):
        r = rank([(acct("hot"), snap((95, 3), (10, 48))),
                  (acct("cool"), snap((10, 3), (10, 48)))])
        self.assertEqual(r[0], "cool")

    def test_hot_short_resetting_soon_is_usable(self):
        # hot but resets in 30 min: should be about as good as cool, not penalised
        r = rank([(acct("hot_soon"), snap((95, 0.5), (10, 12))),
                  (acct("cool"), snap((10, 3), (10, 160)))])
        self.assertEqual(r[0], "hot_soon")

    def test_hot_short_resets_in_exactly_grace(self):
        r = rank([(acct("edge"), snap((95, 1.0), (10, 12))),
                  (acct("cool"), snap((10, 3), (10, 160)))])
        self.assertIn(r[0], {"edge", "cool"})  # boundary: document which

    def test_hot_short_at_exactly_85(self):
        r = rank([(acct("edge"), snap((85.0, 3), (10, 48))),
                  (acct("cool"), snap((84.9, 3), (10, 48)))])
        self.assertEqual(r[0], "cool")

    def test_all_hot_prefers_soonest_short_reset(self):
        r = rank([(acct("late"), snap((95, 4), (50, 48))),
                  (acct("soon"), snap((95, 0.2), (50, 48)))])
        self.assertEqual(r[0], "soon")

    def test_short_rule_does_not_defeat_weekly_wildly(self):
        # A: short hot (reset 3h) but weekly expires in 2h with 90% of a 20x left.
        # B: short fine, weekly nearly spent (5% left of 1x), 6 days to go.
        # Weekly is PRIMARY: A holds a vast wasted budget. Expect A.
        r = rank([(acct("A", 20), snap((95, 3), (10, 2))),
                  (acct("B", 1), snap((5, 3), (95, 144)))])
        self.assertEqual(r[0], "A")

    def test_short_hot_penalty_does_not_starve_forever(self):
        # hot short with a plentiful, soon-expiring week vs a marginal cool one
        r = rank([(acct("A"), snap((90, 4.5), (0, 3))),
                  (acct("B"), snap((0, 4.5), (60, 100)))])
        # documenting; no assert on winner beyond completeness
        self.assertEqual(set(r), {"A", "B"})


class StaleAbsent(unittest.TestCase):
    def test_no_snapshot_does_not_crash(self):
        r = rank([(acct("never"), None), (acct("known"), snap((0, 4), (50, 50)))])
        self.assertEqual(set(r), {"never", "known"})

    def test_never_measured_not_ranked_below_exhausted(self):
        r = rank([(acct("never"), None), (acct("full"), snap((0, 4), (100, 50)))])
        self.assertEqual(r[0], "never")

    def test_never_measured_not_always_first_over_healthy_idle(self):
        r = rank([(acct("never"), None), (acct("idle"), snap((0, 4), (0, 12)))])
        # unknown should not beat a known fully idle, soon-to-reset account
        self.assertEqual(r[0], "idle")

    def test_all_unmeasured(self):
        r = rank([(acct("a"), None), (acct("b"), None)])
        self.assertEqual(set(r), {"a", "b"})

    def test_snapshot_with_empty_windows(self):
        r = rank([(acct("a"), Snapshot(NOW, {})), (acct("b"), snap((0, 4), (50, 50)))])
        self.assertEqual(set(r), {"a", "b"})

    def test_missing_long_window_only(self):
        r = rank([(acct("a"), snap((10, 3), None)), (acct("b"), snap((10, 3), (50, 50)))])
        self.assertEqual(set(r), {"a", "b"})

    def test_missing_short_window_only(self):
        r = rank([(acct("a"), snap(None, (50, 50))), (acct("b"), snap((10, 3), (50, 50)))])
        self.assertEqual(set(r), {"a", "b"})

    def test_rolled_over_long_window_counts_as_empty(self):
        # long window's resets_at in the past: usage has reset to 0
        s_old = Snapshot(NOW - 10 * H, {SHORT: Window(0, NOW + 4 * H),
                                        LONG: Window(99, NOW - 1 * H)})
        r = rank([(acct("rolled"), s_old), (acct("mid"), snap((0, 4), (50, 100)))])
        self.assertEqual(r[0], "rolled")

    def test_resets_at_exactly_now_counts_as_rolled(self):
        s = Snapshot(NOW - H, {SHORT: Window(0, NOW + 4 * H), LONG: Window(99, NOW)})
        r = rank([(acct("edge"), s), (acct("mid"), snap((0, 4), (50, 100)))])
        self.assertEqual(r[0], "edge")

    def test_rolled_short_exhausted_long_stays_exhausted(self):
        s = Snapshot(NOW - 6 * H, {SHORT: Window(99, NOW - 1 * H), LONG: Window(99, NOW + 48 * H)})
        r = rank([(acct("full"), s), (acct("ok"), snap((0, 4), (90, 100)))])
        self.assertEqual(r[0], "ok")

    def test_stale_snapshot_old_but_windows_future_still_usable(self):
        # captured 3 days ago; windows not yet reset. Must not crash; ideally treated as stale-but-valid
        s = Snapshot(NOW - 72 * H, {SHORT: Window(10, NOW + 3 * H), LONG: Window(10, NOW + 48 * H)})
        r = rank([(acct("old"), s), (acct("new"), snap((10, 3), (10, 48)))])
        self.assertEqual(set(r), {"old", "new"})


class Starvation(unittest.TestCase):
    def test_simulated_week_uses_both_accounts_fully(self):
        """Greedy: each hour pick top, burn 0.6%/h of that account's weekly budget (cap=1).
        Reset times differ. Neither account should sit idle (starved) for the week
        nor be left with big leftover at reset."""
        state = {"A": [0.0, 24.0], "B": [0.0, 96.0]}  # [used_pct, hours_to_reset]
        picks = {"A": 0, "B": 0}
        for hour in range(168):
            entries = [(acct(k), snap((0, 4), (v[0], v[1]))) for k, v in state.items()]
            top = rank(entries)[0]
            picks[top] += 1
            state[top][0] = min(100.0, state[top][0] + 0.6 * 1.0 * 4)
            for k, v in state.items():
                v[1] -= 1
                if v[1] <= 0:
                    v[0], v[1] = 0.0, 168.0
        self.assertGreater(picks["A"], 20, picks)
        self.assertGreater(picks["B"], 20, picks)
