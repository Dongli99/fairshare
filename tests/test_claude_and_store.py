import os
import subprocess
import sys
import tempfile
import time
import json
import unittest
from pathlib import Path
from unittest import mock

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


class WrongTypes(unittest.TestCase):
    """A snapshot written with wrong types (a hand edit, another format) loads without the
    windows it cannot use, so nothing crashes and the next probe refills them."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = mock.patch.dict(os.environ, {"FAIRSHARE_STATE": d.name})
        env.start()
        self.addCleanup(env.stop)
        self.file = Path(d.name) / "snapshots" / "claude.json"
        self.file.parent.mkdir()

    def test_the_reviews_repro_drops_the_window_and_a_probe_heals_it(self):
        self.file.write_text('{"captured_at": 1, "windows": {"seven_day": '
                             '{"used_pct": 50, "resets_at": null}}}')
        self.assertEqual(store.load("claude"), Snapshot(1.0, {}))
        store.record("claude", {"seven_day": Window(41.0, 900.0)}, 100.0, probe=True)
        self.assertEqual(store.load("claude").windows, {"seven_day": Window(41.0, 900.0)})

    def test_each_wrong_type_drops_only_its_window(self):
        self.file.write_text(json.dumps({"captured_at": 1, "windows": {
            "five_hour": {"used_pct": "50", "resets_at": 9}, "seven_day": {"used_pct": 5,
                                                                          "resets_at": 9},
            "x": [1, 2], "y": {"used_pct": True, "resets_at": 9}, "z": {"used_pct": 1}}}))
        self.assertEqual(store.load("claude"), Snapshot(1.0, {"seven_day": Window(5, 9)}))

    def test_a_wrong_capture_time_or_window_table_reads_as_no_snapshot(self):
        for text in ('{"captured_at": "1", "windows": {}}', '{"captured_at": 1, "windows": []}',
                     '[]'):
            self.file.write_text(text)
            self.assertIsNone(store.load("claude"), text)


class UsageLog(unittest.TestCase):
    """Each new snapshot is appended to the account's usage log with the cost its sessions drew
    since the one before; a redraw that changes nothing writes no line."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = mock.patch.dict(os.environ, {"FAIRSHARE_STATE": d.name})
        env.start()
        self.addCleanup(env.stop)
        self.log = Path(d.name) / "usage" / "a.jsonl"
        recs = [mock.Mock(account="a", id="s1", costs=[[[100.0, 0.0]]]),
                mock.Mock(account="a", id="s2", costs=[]),                     # no cost data
                mock.Mock(account="b", id="s3", costs=[[[100.0, 0.0], [150.0, 9.0]]])]
        self.recs = recs
        p = mock.patch.object(store, "sessions", lambda: recs)
        p.start()
        self.addCleanup(p.stop)

    def lines(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_lines_carry_the_windows_and_the_cost_since_the_last(self):
        store.record("a", {"seven_day": Window(40.0, 900.0)}, 120.0)
        store.record("a", {"seven_day": Window(40.0, 900.0)}, 200.0)      # unchanged: no line
        self.recs[0].costs = [[[100.0, 0.0], [150.0, 1.5], [250.0, 4.0]]]
        store.record("a", {"seven_day": Window(42.0, 900.0)}, 260.0)
        self.assertEqual(self.lines(), [
            {"t": 120.0, "cost": None, "windows": {"seven_day": [40.0, 900.0]}},
            {"t": 260.0, "cost": 4.0, "windows": {"seven_day": [42.0, 900.0]}}])

    def test_a_cost_point_that_grows_after_a_line_counts_in_the_next(self):
        """The newest cost point keeps growing in place (COST_POINT_S): what it gains after a
        line was written belongs to the next line, and nothing is counted twice."""
        rec = mock.Mock(account="a", id="s1", costs=[[[100.0, 0.0]]])
        with mock.patch.object(store, "sessions", lambda: [rec]):
            store.record("a", {"seven_day": Window(40.0, 900.0)}, 120.0)
            rec.costs = [[[100.0, 0.0], [150.0, 1.0]]]
            store.record("a", {"seven_day": Window(41.0, 900.0)}, 160.0)      # counts 1.0
            rec.costs = [[[100.0, 0.0], [150.0, 3.0]]]                        # grew in place
            store.record("a", {"seven_day": Window(42.0, 900.0)}, 200.0)      # counts 2.0
            rec.costs = [[[100.0, 0.0], [150.0, 3.5], [460.0, 5.0]]]          # grew, then a point
            store.record("a", {"seven_day": Window(43.0, 900.0)}, 470.0)      # counts 2.0
        self.assertEqual([x["cost"] for x in self.lines()], [None, 1.0, 2.0, 2.0])


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

    def test_an_already_expired_window_never_replaces_a_live_one(self):
        """An idle session redraws a window that reset after it last heard: never taken."""
        live = Snapshot(100.0, {"five_hour": Window(96, 20_000), "seven_day": Window(5, 500_000)})
        m = store.merge(live, {"five_hour": Window(70, 9_400),
                               "seven_day": Window(97, 9_000)}, 10_000.0)
        self.assertEqual(m.windows, live.windows)
        self.assertEqual(m.captured_at, 100.0)

    def test_an_already_expired_window_is_not_recorded_when_nothing_is_stored(self):
        self.assertEqual(store.merge(None, {"five_hour": Window(70, 9_400)}, 10_000.0).windows, {})

    def test_an_all_stale_redraw_keeps_the_snapshot_time(self):
        m = store.merge(self.OLD, {"seven_day": Window(50, 500_000)}, 200.0)
        self.assertEqual(m.captured_at, 100.0)

    def test_a_probe_lower_in_the_same_window_is_taken(self):
        """Usage can fall inside a window (the provider credits some back): the provider's own
        usage probe is current by construction, so its lower value replaces the stored one."""
        old = Snapshot(100.0, {**self.OLD.windows, "seven_day:Fable": Window(91, 500_000)})
        m = store.merge(old, {"seven_day": Window(34, 500_000 - 60),     # reset to the minute
                              "seven_day:Fable": Window(0, 500_000)}, 200.0, probe=True)
        self.assertEqual(m.windows["seven_day"], Window(34, 500_000))     # the exact reset kept
        self.assertEqual(m.windows["seven_day:Fable"], Window(0, 500_000))
        self.assertEqual(m.windows["five_hour"], Window(40, 10_000))    # omitted: kept
        self.assertEqual(m.captured_at, 200.0)

    def test_a_probe_a_point_lower_is_rounding(self):
        m = store.merge(self.OLD, {"seven_day": Window(59, 500_000)}, 200.0, probe=True)
        self.assertEqual(m.windows["seven_day"], Window(60, 500_000))

    def test_a_probe_never_takes_an_already_expired_window(self):
        m = store.merge(self.OLD, {"five_hour": Window(3, 9_000)}, 9_500.0, probe=True)
        self.assertEqual(m.windows["five_hour"], Window(40, 10_000))



class ConcurrentCaptures(unittest.TestCase):
    """Captures of one account arriving together must not let an idle redraw undo a live value."""
    RECORD = ("import os, sys, time; sys.path.insert(0, {src!r})\n"
              "from fairshare import store; from fairshare.model import Window\n"
              "while not os.path.exists({go!r}): time.sleep(0.001)\n"
              "store.record('a', {{'seven_day': Window(float(sys.argv[1]), 9e9)}}, time.time())")

    def test_the_highest_value_survives_a_burst(self):
        src = os.path.join(os.path.dirname(__file__), "..", "src")
        for trial in range(5):
            with tempfile.TemporaryDirectory() as d:
                go = os.path.join(d, "go")
                code = self.RECORD.format(src=src, go=go)
                env = {**os.environ, "FAIRSHARE_STATE": d}
                procs = [subprocess.Popen([sys.executable, "-c", code, str(v)], env=env)
                         for v in [80] + list(range(11, 21))]
                time.sleep(0.3)                      # let every process reach the start line
                open(go, "w").close()
                for p in procs:
                    self.assertEqual(p.wait(timeout=20), 0)
                os.environ["FAIRSHARE_STATE"] = d
                self.assertEqual(store.load("a").windows["seven_day"].used_pct, 80, f"trial {trial}")

if __name__ == "__main__":
    unittest.main()
