import os
import tempfile
import unittest

from fairshare import store
from fairshare.model import Window
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


if __name__ == "__main__":
    unittest.main()
