"""Dollars per point of a plan window, learned from the usage log (`rates`), and the room it
gives in `status`. Made-up numbers throughout."""
import json
import time
import unittest
from unittest import mock
from dataclasses import replace

from fairshare import rates, store
from fairshare.model import SessionRecord, Window

from test_honest import H, NOW, ORG, Fleet

R1, R2, R3 = NOW + 5 * H, NOW + 10 * H, NOW + 15 * H     # three short-window resets


def write_log(tmp, id, lines):
    """Account `id`'s usage log (`store.usage_log`) in the state under `tmp`."""
    d = tmp / "state" / "usage"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{id}.jsonl").write_text("".join(
        json.dumps({"t": t, "spent": s, "windows": {k: list(v) for k, v in w.items()}}) + "\n"
        for t, s, w in lines))


def rated(tmp, id):
    """A usage log that gives account `id` $0.10 a point of its 5h window (two instances of
    $5.00 for 50 points) and $1.00 a point of its week (10 points for $10.00)."""
    week, old, new = NOW + 50 * H, NOW - 5 * H, NOW + 3 * H
    write_log(tmp, id, [(NOW - 9 * H, None, {"five_hour": (0, old), "seven_day": (30, week)}),
                        (NOW - 8 * H, 5.0, {"five_hour": (50, old), "seven_day": (35, week)}),
                        (NOW - 2 * H, 0.0, {"five_hour": (0, new), "seven_day": (35, week)}),
                        (NOW - H, 5.0, {"five_hour": (50, new), "seven_day": (40, week)})])


def spending(sid, account, dollars):
    """Session `sid` on `account` spent `dollars` in the last hour (its record made if need be)."""
    costs = store.add_cost(store.add_cost([], NOW - 30 * 60, 1.0), NOW - 10 * 60, 1.0 + dollars)
    store.update_session(sid, lambda old: replace(old, costs=costs) if old else
                         SessionRecord(sid, account, None, None, NOW, costs=costs))


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
    FOUND = {"five_hour": rates.Rate(0.1, 3, 0.05, 0.2), "seven_day": rates.Rate(1.0, 1, 1, 1)}

    def test_room_to_the_limit_per_window(self):
        windows = {"five_hour": Window(40, NOW + H), "seven_day": Window(100, NOW + 50 * H)}
        self.assertEqual(rates.room(self.FOUND, windows, NOW),
                         {"five_hour": 6.0, "seven_day": 0.0})   # at the limit: none left
        reset = {"five_hour": Window(40, NOW - 1)}               # reset already: unused
        self.assertEqual(rates.room(self.FOUND, reset, NOW), {"five_hour": 10.0, "seven_day": 100.0})

    def test_the_short_window_binds_and_its_refill_is_capped_by_the_week(self):
        windows = {"five_hour": Window(80, NOW + 2 * H), "seven_day": Window(95, NOW + 50 * H)}
        r = rates.limit(self.FOUND, windows, NOW)
        self.assertEqual((r.binds, r.resets_at), ("five_hour", NOW + 2 * H))
        self.assertAlmostEqual(r.dollars, 2.0)
        self.assertAlmostEqual(r.refill, 5.0)                    # a full 5h is 10.0; the week 5.0
        self.assertEqual(set(r.windows), {"five_hour", "seven_day"})

    def test_the_week_binds_and_its_refill_leaves_the_short_window_binding(self):
        windows = {"five_hour": Window(10, NOW + 2 * H), "seven_day": Window(99, NOW + 50 * H)}
        r = rates.limit(self.FOUND, windows, NOW)
        self.assertEqual((r.binds, r.resets_at), ("seven_day", NOW + 50 * H))
        self.assertAlmostEqual(r.dollars, 1.0)
        self.assertAlmostEqual(r.refill, 10.0)                   # the 5h has reset by then too

    def test_a_reset_or_absent_binding_window_has_no_refill(self):
        r = rates.limit({"five_hour": self.FOUND["five_hour"]},
                        {"five_hour": Window(40, NOW - 1)}, NOW)
        self.assertEqual((r.binds, r.dollars, r.resets_at, r.refill),
                         ("five_hour", 10.0, None, None))
        self.assertEqual(rates.limit(self.FOUND, {}, NOW).resets_at, None)

    def test_no_rate_no_room(self):
        self.assertIsNone(rates.limit({}, {"five_hour": Window(99, NOW + H)}, NOW))


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


class Cache(Fleet):
    def test_the_fit_is_kept_until_the_log_grows(self):
        calls = []

        def fit(lines):
            calls.append(len(lines))
            return {"five_hour": [0.1, 2, 0.05, 0.2]}
        self.assertEqual(store.usage_fit("a", fit), {"five_hour": [0.1, 2, 0.05, 0.2]})
        self.assertEqual(calls, [0])                             # no log: computed, not kept
        rated(self.tmp, "a")
        store.usage_fit("a", fit)
        store.usage_fit("a", fit)
        self.assertEqual(calls, [0, 4])                          # kept beside the log
        with open(self.tmp / "state" / "usage" / "a.jsonl", "a") as f:
            f.write(json.dumps({"t": NOW, "spent": 0.0, "windows": {}}) + "\n")
        store.usage_fit("a", fit)
        self.assertEqual(calls, [0, 4, 5])                       # the log grew
        (self.tmp / "state" / "usage" / "a.fit.json").write_text('{"size": 1, "fit": {"x": ["y"]}')
        store.usage_fit("a", fit)
        self.assertEqual(calls, [0, 4, 5, 5])                    # unreadable: again
        store.usage_fit("a", fit)
        self.assertEqual(calls, [0, 4, 5, 5])                    # kept again
        later = time.time() + store.FIT_KEEP_S
        with mock.patch.object(store.time, "time", lambda: later):
            store.usage_fit("a", fit)
        self.assertEqual(calls, [0, 4, 5, 5, 5])                 # an hour old: again


class Status(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", five=(40, 3), seven=(50, 50))
        self.snap("b", five=(10, 3), seven=(20, 50))

    def write_log(self, id, lines):
        write_log(self.tmp, id, lines)

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
        self.assertEqual(out[room[0]], "    room ~$4.50 to the limit (5h binds; refills in "
                                     "3h00m to ~$7.50) · $/pt 5h 0.075 (2 windows, 0.05-0.1), "
                                     "7d 0.3 (1 window)")

    def test_one_window_with_a_rate(self):
        self.write_log("b", [
            (NOW - 9 * H, None, {"seven_day": (5, NOW + 50 * H)}),
            (NOW - 8 * H, 3.0, {"seven_day": (20, NOW + 50 * H)})])
        out = self.go("status")[1]
        self.assertIn("    room ~$16.00 to the limit (7d binds; refills in 50h00m to ~$20.00; "
                      "5h no rate yet) · $/pt 7d 0.2 (1 window)\n", out)


if __name__ == "__main__":
    unittest.main()
