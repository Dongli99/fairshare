import os
import tempfile
import unittest

from fairshare import store
from fairshare.model import Snapshot, Window
from fairshare.providers import claude

PAYLOAD = {"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 200},
                           "seven_day": {"used_percentage": 41.2, "resets_at": 900},
                           "spend_limit": {"used_percentage": 1, "resets_at": 5}}}


class Parse(unittest.TestCase):
    def test_reads_both_windows_and_ignores_spend_limit(self):
        w = claude.parse_capture(PAYLOAD)
        self.assertEqual(set(w), {"five_hour", "seven_day"})
        self.assertEqual(w["seven_day"], Window(41.2, 900.0))

    def test_no_rate_limits_means_nothing_to_record(self):
        self.assertEqual(claude.parse_capture({"model": {}}), {})

    def test_one_window_absent_is_fine(self):
        w = claude.parse_capture({"rate_limits": {"seven_day": PAYLOAD["rate_limits"]["seven_day"]}})
        self.assertEqual(set(w), {"seven_day"})


class Store(unittest.TestCase):
    def test_round_trip_and_merge_keeps_unexpired_window(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["FAIRSHARE_STATE"] = d
            first = store.merge(None, claude.parse_capture(PAYLOAD), 100.0)
            store.save("a", first)
            again = store.merge(store.load("a"), {"five_hour": Window(30, 300)}, 150.0)
            self.assertEqual(again.windows["seven_day"].used_pct, 41.2)   # kept
            self.assertEqual(again.windows["five_hour"].used_pct, 30)     # replaced
            expired = store.merge(again, {}, 1000.0)                      # both past reset
            self.assertEqual(expired.windows, {})


class StaleRedraw(unittest.TestCase):
    """An idle session redraws its statusLine with old values; they must not roll usage back."""
    OLD = Snapshot(100.0, {"five_hour": Window(40, 10_000), "seven_day": Window(60, 500_000)})

    def test_lower_value_in_same_window_is_ignored(self):
        m = store.merge(self.OLD, {"seven_day": Window(55, 500_000)}, 200.0)
        self.assertEqual(m.windows["seven_day"].used_pct, 60)

    def test_reset_time_jitter_is_still_the_same_window(self):
        m = store.merge(self.OLD, {"seven_day": Window(55, 500_000 - 60)}, 200.0)
        self.assertEqual(m.windows["seven_day"], Window(60, 500_000))

    def test_higher_or_equal_value_is_taken(self):
        m = store.merge(self.OLD, {"seven_day": Window(62, 500_000),
                                   "five_hour": Window(40, 10_030)}, 200.0)
        self.assertEqual(m.windows["seven_day"].used_pct, 62)
        self.assertEqual(m.windows["five_hour"].resets_at, 10_030)
        self.assertEqual(m.captured_at, 200.0)

    def test_lower_value_in_a_new_window_is_taken(self):
        m = store.merge(self.OLD, {"seven_day": Window(2, 500_000 + 7 * 86_400)}, 200.0)
        self.assertEqual(m.windows["seven_day"].used_pct, 2)

    def test_lower_value_after_the_old_window_expired_is_taken(self):
        m = store.merge(self.OLD, {"five_hour": Window(3, 10_500)}, 10_001.0)
        self.assertEqual(m.windows["five_hour"], Window(3, 10_500))

    def test_stale_window_does_not_block_a_good_one_in_the_same_payload(self):
        m = store.merge(self.OLD, {"seven_day": Window(50, 500_000),
                                   "five_hour": Window(45, 10_000)}, 200.0)
        self.assertEqual(m.windows["seven_day"].used_pct, 60)
        self.assertEqual(m.windows["five_hour"].used_pct, 45)
        self.assertEqual(m.captured_at, 200.0)

    def test_an_all_stale_redraw_keeps_the_snapshot_time(self):
        m = store.merge(self.OLD, {"seven_day": Window(50, 500_000)}, 200.0)
        self.assertEqual(m.captured_at, 100.0)


if __name__ == "__main__":
    unittest.main()
