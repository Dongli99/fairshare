"""Dollars per point of a plan window, learned from the usage log (`rates`), and the room it
gives in `status`. Made-up numbers throughout."""
import json
import unittest

from fairshare import rates, store
from fairshare.model import Window

from test_honest import H, NOW, ORG, Fleet

R1, R2, R3 = NOW + 5 * H, NOW + 10 * H, NOW + 15 * H     # three short-window resets


def log(*steps, key="five_hour", start=NOW - 20 * H):
    """Log lines, one per (spent, used %, reset), an hour apart; spent None for the first."""
    return [(start + i * H, spent, {key: (used, at)}) for i, (spent, used, at) in enumerate(steps)]


class Fit(unittest.TestCase):
    def test_pooled_rate_count_and_spread(self):
        lines = log((None, 0, R1), (1.0, 10, R1),               # 1.0 for 10 points: 0.1
                    (0.5, 0, R2), (2.0, 10, R2), (2.0, 20, R2))  # reset pair dropped; 0.2
        r = rates.fit(lines, "five_hour", NOW, 2)
        self.assertEqual((r.instances, r.low, r.high), (2, 0.1, 0.2))
        self.assertAlmostEqual(r.per_pct, 5.0 / 30)              # pooled, not averaged

    def test_a_reset_between_two_lines_drops_that_lines_spend(self):
        lines = log((None, 0, R1), (1.0, 10, R1), (9.0, 20, R2), (2.0, 30, R2))
        r = rates.fit(lines, "five_hour", NOW, 1)
        self.assertAlmostEqual(r.per_pct, 3.0 / 20)              # the 9.0 is not counted
        self.assertEqual(r.instances, 2)

    def test_resets_given_to_the_minute_stay_one_instance(self):
        lines = log((None, 0, R1), (1.0, 6, R1 + 60), (1.0, 12, R1), (1.0, 18, R1 + 120))
        r = rates.fit(lines, "five_hour", NOW, 1)
        self.assertEqual((r.instances, r.per_pct), (1, 3.0 / 18))

    def test_a_line_without_the_window_breaks_the_pair(self):
        lines = log((None, 0, R1), (1.0, 10, R1), (1.0, 20, R1), (1.0, 30, R1))
        lines[2] = (lines[2][0], 1.0, {})
        r = rates.fit(lines, "five_hour", NOW, 1)
        self.assertEqual(r.per_pct, 1.0 / 10)                    # the gap and the line after drop

    def test_a_credit_back_counts_as_no_rise(self):
        lines = log((None, 0, R1), (1.0, 20, R1), (0.0, 15, R1), (1.0, 25, R1))
        r = rates.fit(lines, "five_hour", NOW, 1)
        self.assertAlmostEqual(r.per_pct, 2.0 / 30)              # 20 + 0 + 10, not 25

    def test_too_little_data_gives_no_rate(self):
        one = log((None, 0, R1), (1.0, 20, R1))
        self.assertIsNone(rates.fit(one, "five_hour", NOW, 2))              # one instance
        self.assertIsNotNone(rates.fit(one, "five_hour", NOW, 1))
        small = log((None, 0, R1), (1.0, 2, R1), (None, 0, R2), (1.0, 20, R2),
                    (None, 0, R3), (1.0, 2.5, R3))
        r = rates.fit(small, "five_hour", NOW, 1)
        self.assertEqual(r.instances, 1)                         # rises under 3 points left out
        self.assertIsNone(rates.fit(small, "five_hour", NOW, 2))
        low = log((None, 0, R1), (1.0, 4, R1), (None, 0, R2), (1.0, 4, R2))
        self.assertIsNone(rates.fit(low, "five_hour", NOW, 2))   # 8 points in all
        self.assertIsNone(rates.fit([], "five_hour", NOW, 1))

    def test_lines_older_than_two_weeks_are_left_out(self):
        old = log((None, 0, R1), (5.0, 10, R1), start=NOW - 15 * 24 * H)
        new = log((None, 0, R2), (1.0, 10, R2), (None, 0, R3), (1.0, 10, R3))
        r = rates.fit(old + new, "five_hour", NOW, 2)
        self.assertEqual((r.instances, r.per_pct), (2, 0.1))

    def test_fit_plan_needs_two_short_instances_or_one_long(self):
        week = NOW + 100 * H
        lines = [(NOW - 3 * H, None, {"five_hour": (0, R1), "seven_day": (10, week)}),
                 (NOW - 2 * H, 1.5, {"five_hour": (30, R1), "seven_day": (13, week),
                                     "seven_day:Model": (50, week)}),
                 (NOW - H, 3.0, {"five_hour": (60, R1), "seven_day": (20, week)})]
        found = rates.fit_plan(lines, "five_hour", "seven_day", NOW)
        self.assertEqual(list(found), ["seven_day"])
        self.assertAlmostEqual(found["seven_day"].per_pct, 4.5 / 10)


class Room(unittest.TestCase):
    def test_room_before_the_warning_level_per_window(self):
        found = {"five_hour": rates.Rate(0.1, 3, 0.05, 0.2), "seven_day": rates.Rate(1.0, 1, 1, 1)}
        windows = {"five_hour": Window(40, NOW + H), "seven_day": Window(95, NOW + 50 * H)}
        self.assertEqual(rates.room(found, windows, 90, NOW),
                         {"five_hour": 5.0, "seven_day": 0.0})   # past the level: none left
        reset = {"five_hour": Window(40, NOW - 1)}               # reset already: unused
        self.assertEqual(rates.room(found, reset, 90, NOW), {"five_hour": 9.0, "seven_day": 90.0})


class Reader(Fleet):
    def test_bad_lines_and_windows_are_skipped(self):
        d = self.tmp / "state" / "usage"
        d.mkdir(parents=True)
        (d / "a.jsonl").write_text("\n".join([
            json.dumps({"t": 1, "spent": None, "windows": {"five_hour": [5, 100]}}),
            "{torn",
            json.dumps({"t": "x", "spent": 1, "windows": {}}),
            json.dumps({"t": 2, "spent": "x", "windows": {}}),
            json.dumps([1, 2]),
            json.dumps({"t": 3, "spent": 0.5, "windows": {"five_hour": [7, 100],
                                                          "seven_day": ["x", 1], "w": [1]}}),
        ]) + "\n")
        self.assertEqual(store.usage_log("a"), [(1.0, None, {"five_hour": (5.0, 100.0)}),
                                               (3.0, 0.5, {"five_hour": (7.0, 100.0)})])
        self.assertEqual(store.usage_log("none"), [])


class Status(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", five=(40, 3), seven=(50, 50))
        self.snap("b", five=(10, 3), seven=(20, 50))

    def write_log(self, id, lines):
        d = self.tmp / "state" / "usage"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{id}.jsonl").write_text("".join(
            json.dumps({"t": t, "spent": s, "windows": {k: list(v) for k, v in w.items()}}) + "\n"
            for t, s, w in lines))

    def test_room_line_under_an_account_with_a_rate_only(self):
        week = NOW + 50 * H
        self.write_log("a", [
            (NOW - 9 * H, None, {"five_hour": (0, NOW - 5 * H), "seven_day": (30, week)}),
            (NOW - 8 * H, 2.0, {"five_hour": (20, NOW - 5 * H), "seven_day": (40, week)}),
            (NOW - 2 * H, 3.0, {"five_hour": (10, NOW + 3 * H), "seven_day": (45, week)}),
            (NOW - H, 1.0, {"five_hour": (30, NOW + 3 * H), "seven_day": (50, week)})])
        out = self.go("status")[1].splitlines()
        a = next(i for i, l in enumerate(out) if l[2:].startswith("a "))
        end = next((i for i, l in enumerate(out) if i > a and not l.startswith("    ")), len(out))
        room = [i for i, l in enumerate(out) if l.startswith("    room ~$")]
        self.assertEqual(len(room), 1)                           # b has no log: no line
        self.assertTrue(a < room[0] < end, out)                  # among a's notes
        self.assertEqual(out[room[0]], "    room ~$3.75 before the 90% warning (5h binds; 7d "
                                     "~$12.00) · $/pt 5h 0.075 (2 windows, 0.05-0.1), 7d 0.3 "
                                     "(1 window)")

    def test_one_window_with_a_rate(self):
        self.write_log("b", [
            (NOW - 9 * H, None, {"seven_day": (5, NOW + 50 * H)}),
            (NOW - 8 * H, 3.0, {"seven_day": (20, NOW + 50 * H)})])
        out = self.go("status")[1]
        self.assertIn("    room ~$14.00 before the 90% warning (7d binds; 5h no rate yet) · "
                      "$/pt 7d 0.2 (1 window)\n", out)


if __name__ == "__main__":
    unittest.main()
