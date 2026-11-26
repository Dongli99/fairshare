"""The two stages (policy.handoff_due) and where a handed-off session may go (policy.can_take).
Room is counted in percent of a Pro short window, on both windows."""
import unittest

from fairshare.model import Snapshot
from fairshare.policy import HANDOFF_ROOM, can_take, handoff_due, room

from test_policy import H, MAX, NOW, PRO, ULTRA, acct, snap

M = 60.0


class Room(unittest.TestCase):
    def test_short_window_counts_in_pro_points(self):
        self.assertEqual(room(acct("p"), snap((90, 2), (10, 100)), NOW), (10.0, NOW + 2 * H))
        self.assertEqual(room(acct("m", MAX), snap((98, 2), (10, 100)), NOW)[0], 10.0)

    def test_week_counts_by_how_much_of_it_one_short_window_uses(self):
        r, at = room(acct("p"), snap((10, 2), (99, 100)), NOW)     # 1% of a week = 100/13 points
        self.assertAlmostEqual(r, 100 / PRO.per_short)
        self.assertEqual(at, NOW + 100 * H)

    def test_a_window_past_its_reset_or_absent_counts_as_unused(self):
        self.assertEqual(room(acct("p"), Snapshot(NOW, {}), NOW), (100.0, None))
        self.assertEqual(room(acct("p"), snap((95, -1), (10, 100)), NOW), (100.0, None))
        self.assertEqual(room(acct("p"), snap((95, -1), (95, 100)), NOW), (500 / 13, NOW + 100 * H))
        self.assertEqual(room(acct("u", ULTRA), snap(None, (50, 80)), NOW), (1000.0, None))
        self.assertEqual(room(acct("u", ULTRA), snap(None, (99.9, 80)), NOW)[1], NOW + 80 * H)

    def test_no_snapshot_is_a_fresh_account(self):
        self.assertEqual(room(acct("p"), None, NOW), (100.0, None))


class HandoffDue(unittest.TestCase):
    def test_continue_until_ten_points_of_a_pro_short_window_are_left(self):
        self.assertFalse(handoff_due(acct("p"), snap((89, 2), (10, 100)), NOW))
        self.assertTrue(handoff_due(acct("p"), snap((90, 2), (10, 100)), NOW))
        self.assertEqual(HANDOFF_ROOM, 10.0)

    def test_a_bigger_plan_hands_off_at_the_same_amount(self):
        self.assertFalse(handoff_due(acct("m", MAX), snap((97, 2), (10, 100)), NOW))
        self.assertTrue(handoff_due(acct("m", MAX), snap((98, 2), (10, 100)), NOW))

    def test_the_week_binds_when_it_runs_out_first(self):
        self.assertFalse(handoff_due(acct("p"), snap((10, 2), (98, 100)), NOW))
        self.assertTrue(handoff_due(acct("p"), snap((10, 2), (99, 100)), NOW))

    def test_a_reset_under_half_an_hour_away_lets_the_limit_wait_instead(self):
        self.assertFalse(handoff_due(acct("p"), snap((95, 29 * M / H), (10, 100)), NOW))
        self.assertTrue(handoff_due(acct("p"), snap((95, 31 * M / H), (10, 100)), NOW))

    def test_a_soon_reset_that_frees_nothing_still_hands_off(self):
        """The 5h resets in 20 min, but the week is nearly out: the reset does not help."""
        self.assertTrue(handoff_due(acct("p"), snap((95, 20 * M / H), (99, 100)), NOW))


class CanTake(unittest.TestCase):
    """The owner's points: 30 left with an hour or more to its reset, 20 with 45 min, 10 with
    30 min: a line through them, capped at 30."""
    def test_the_owners_points(self):
        for left, minutes in ((30, 61), (20, 45), (10, 30)):
            with self.subTest(left=left, minutes=minutes):
                self.assertTrue(can_take(acct("p"), snap((100 - left, minutes * M / H),
                                                         (10, 100)), NOW))
                self.assertFalse(can_take(acct("p"), snap((101 - left, minutes * M / H),
                                                          (10, 100)), NOW))

    def test_more_than_thirty_points_is_enough_however_far_the_reset(self):
        self.assertTrue(can_take(acct("p"), snap((70, 4.9), (10, 100)), NOW))
        self.assertTrue(can_take(acct("p"), None, NOW))

    def test_a_spent_account_cannot_take_work_even_just_before_its_reset(self):
        self.assertFalse(can_take(acct("p"), snap((100, 5 * M / H), (10, 100)), NOW))

    def test_a_target_whose_week_would_bind_right_after_its_reset_cannot(self):
        """The 5h is fine for its 30 min, but the week leaves 2% (15 points) after it."""
        self.assertFalse(can_take(acct("p"), snap((90, 30 * M / H), (98, 100)), NOW))
        self.assertTrue(can_take(acct("p"), snap((90, 30 * M / H), (90, 100)), NOW))
