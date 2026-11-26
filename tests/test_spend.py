"""Stage 5c, part A: what each session spends. The cost series is pure (store.add_cost,
store.spend); the rest runs on the fleets of test_handover and test_workers (temp HOME, config,
state and hub; fakes first on PATH, proved in Fleet.setUp; inherited CLAUDE*, FAIRSHARE_* and
BROWSER cleared; the clock injected)."""
import io
import json
import os
import sys
import unittest
from unittest import mock

from fairshare import cli, rates, store
from fairshare.model import ROLE_WORKER, SessionRecord, SessionSeen, Worker

from test_handover import Handover
from test_honest import H, NOW
from test_pickup import SID
from test_workers import PARENT, Work

M = 60.0


class Series(unittest.TestCase):
    def feed(self, *points, series=None):
        series = series or []
        for t, c in points:
            series = store.add_cost(series, t, c)
        return series

    def test_a_point_when_moved_and_the_last_is_five_minutes_old(self):
        s = self.feed((0, 1.0), (60, 1.5))                       # the base is never changed
        self.assertEqual(s, [[[0, 1.0], [60, 1.5]]])
        s = self.feed((120, 2.0), series=s)                      # last 1 min old: its value
        self.assertEqual(s, [[[0, 1.0], [60, 2.0]]])
        s = self.feed((120 + 4 * M, 2.0), series=s)              # not moved: nothing
        self.assertEqual(s, [[[0, 1.0], [60, 2.0]]])
        s = self.feed((60 + 5 * M, 2.5), series=s)               # 5 min: a new point
        self.assertEqual(s, [[[0, 1.0], [60, 2.0], [360, 2.5]]])

    def test_the_latest_value_is_always_kept(self):
        s = self.feed((0, 1.0), (10 * M, 2.0), (11 * M, 2.1), (12 * M, 2.2))
        self.assertEqual(s[-1][-1], [10 * M, 2.2])
        self.assertEqual(store.total_cost(s), 2.2)

    def test_two_hours_are_kept_with_one_base_point_before(self):
        s = self.feed(*[(i * 10 * M, float(i)) for i in range(20)])     # 0 .. 190 min
        self.assertEqual([p[0] for p in s[0]], [i * 10 * M for i in range(7, 20)])
        self.assertEqual(s[0][0][0], 190 * M - 2 * H)            # the base: two hours back
        self.assertEqual(store.spend(s, 190 * M - 2 * H, 190 * M), 12.0)  # nothing lost inside

    def test_an_old_lone_latest_stays(self):
        s = self.feed((0, 1.0), (5 * H, 1.0))
        self.assertEqual(s, [[[0, 1.0]]])

    def test_a_drop_starts_a_new_series_and_never_spends_negative(self):
        s = self.feed((0, 5.0), (10 * M, 6.0), (20 * M, 0.5), (30 * M, 1.5))
        self.assertEqual(s, [[[0, 5.0], [10 * M, 6.0]], [[20 * M, 0.5], [30 * M, 1.5]]])
        self.assertEqual(store.spend(s, 0, H), 2.0)              # 1.0 + 1.0; the fall counts 0
        self.assertEqual(store.total_cost(s), 7.5)
        old = self.feed(*[(i * 10 * M, 1.0 + i) for i in range(3)])
        old = self.feed((3 * H, 0.2), series=old)                # the old series is past 2 h
        self.assertEqual(old, [[[3 * H, 0.2]]])

    def test_spend_in_a_span(self):
        s = self.feed((0, 1.0), (10 * M, 2.0), (20 * M, 4.0), (30 * M, 7.0))
        self.assertEqual(store.spend(s, 0, 30 * M), 6.0)
        self.assertEqual(store.spend(s, 15 * M, 30 * M), 5.0)    # counted at the later point
        self.assertEqual(store.spend(s, 30 * M, H), 0)
        self.assertIsNone(store.spend([], 0, H))


class Capture(Handover):
    def capture(self, sid=SID, account="a", **payload):
        raw = json.dumps({"session_id": sid, "cwd": str(self.work), **payload})
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw.encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR":
                                             str(self.tmp / "hub/accounts" / account)}):
            return self.go("capture")

    def test_capture_keeps_the_series_and_a_payload_without_cost_keeps_it(self):
        self.capture(cost={"total_cost_usd": 0.5, "total_duration_ms": 1000})
        self.clock[0] += 6 * M
        self.capture(cost={"total_cost_usd": 0.75})
        self.capture()
        self.capture(cost={"total_cost_usd": "x"})
        self.assertEqual(store.session(SID).costs, [[[NOW, 0.5], [NOW + 6 * M, 0.75]]])
        self.assertEqual(json.loads((self.tmp / "state/sessions" / f"{SID}.json").read_text())
                         ["costs"], [[[NOW, 0.5], [NOW + 6 * M, 0.75]]])


def costed(sid, account, rises, cwd="/w/proj", seen=NOW, role=None, parent=None, model="Opus"):
    """A session record whose cost rose by `rises` (one per 10 minutes, ending now)."""
    series, c, t0 = [], 1.0, NOW - 10 * M * len(rises)
    series = store.add_cost(series, t0, c) if rises else []
    for i, r in enumerate(rises, 1):
        c += r
        series = store.add_cost(series, t0 + 10 * M * i, c)
    store.update_session(sid, lambda old: SessionRecord(sid, account, cwd, None, seen,
                                                        role=role, parent=parent,
                                                        model_name=model, costs=series))


class Status(Handover):
    def block(self, account="b"):
        code, out, _ = self.go("status")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        i = next(i for i, l in enumerate(lines)
                 if not l.startswith("    ") and l[2:].split()[:1] == [account])
        block = []
        for l in lines[i + 1:]:
            if not l.startswith("    "):
                break
            if l.strip().startswith(("session ", "+")):
                block.append(l.strip())
        return block

    def test_heaviest_first_with_shares_roles_names_and_no_data(self):
        costed("s-main-1", "b", [0.5, 0.5, 1.0])                  # 2.0 in the hour
        costed("s-wrk-22", "b", [3.0, 3.0], role=ROLE_WORKER, parent="s-main-1", model=None,
               cwd="/w/other")                                    # 6.0
        store.add_worker(Worker("w-s-main-1-1", "s-main-1", "b", "/w/other", NOW - H,
                                "s-wrk-22"))
        costed("s-quiet3", "b", [], model="Haiku")                # no cost data
        costed("s-gone-44", "b", [9.0], seen=NOW - 11 * M)        # not live, but in the total
        self.assertEqual(self.block(), [
            "session s-wrk-22 · - · other · w-s-main-1-1 · $6.00 (35%)",
            "session s-main-1 · Opus · proj · main · $2.00 (12%)",
            "session s-quiet3 · Haiku · proj · main · -"])

    def test_at_most_five_then_more(self):
        for i in range(7):
            costed(f"s-{i}", "b", [float(i + 1)])
        block = self.block()
        self.assertEqual([l.split()[1] for l in block[:5]], ["s-6", "s-5", "s-4", "s-3", "s-2"])
        self.assertEqual(block[5:], ["+2 more"])

    def test_live_means_seen_in_the_last_ten_minutes_and_per_account(self):
        costed("s-in", "b", [1.0], seen=NOW - 10 * M)
        costed("s-out", "b", [1.0], seen=NOW - 10 * M - 1)
        costed("s-on-c", "c", [1.0])
        self.assertEqual([l.split()[1] for l in self.block()], ["s-in"])
        self.assertEqual([l.split()[1] for l in self.block("c")], ["s-on-c"])
        self.assertEqual(self.block("a"), [])


class Heaviest(Handover):
    def text(self, me="s-me"):
        accounts = cli._load()[0]
        acct = next(a for a in accounts if a.id == "b")
        rec = store.session(me)
        room = rates.Room(0.5, "five_hour", NOW + 2 * H, 5.0, {"five_hour": 0.5})
        return cli._warning_text(rec, acct, (room, 2.0), accounts, cli._load()[1], NOW)

    def test_added_at_half_or_more_absent_below(self):
        costed("s-me", "b", [1.0])
        costed("s-big", "b", [1.0], cwd="/w/api")
        self.assertIn("to about $5.00. Heaviest in the last hour: session s-big (Opus, api, "
                      "50%). ", self.text())
        costed("s-big", "b", [0.9], cwd="/w/api")
        self.assertNotIn("Heaviest", self.text())
        costed("s-me", "b", [9.0])                                # its own spend never counts
        self.assertNotIn("Heaviest", self.text())


class WorkerCost(Work):
    """The worker's measured cost, on its end (watch) and after 30 minutes still."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(cli, "_start_probe", lambda *a: None)
        p.start()
        self.addCleanup(p.stop)
        self.w = Worker("w-1", PARENT, "b", str(self.here), NOW - H, "s-w-1")
        store.add_worker(self.w)
        cli._joined(self.w, "s-w-1", NOW)
        store.update_session("s-w-1", lambda old: SessionRecord(
            **{**vars(old), "costs": [[[NOW - 50 * M, 0.5], [NOW - 20 * M, 2.5]]]}))

    def watch(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": PARENT,
                                          "CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            return self.go("watch", "--timeout", "1")

    def test_recorded_when_it_ended(self):
        self.assertEqual(self.watch()[1], "worker w-1 ended\n")
        w = store.worker("w-1")
        self.assertEqual((w.cost, w.duration), (2.5, 40 * M))

    def test_recorded_after_thirty_minutes_still(self):
        (self.log / "live").write_text("w-1\n")                     # it runs on
        self.watch()
        self.assertEqual(store.worker("w-1").cost, None)              # 20 minutes: not yet
        self.clock[0] += 10 * M
        self.watch()
        w = store.worker("w-1")
        self.assertEqual((w.cost, w.duration), (2.5, 40 * M))

    def test_the_main_sessions_prompt_hook_records_it_too(self):
        self.clock[0] += 10 * M
        store.note_session(SessionSeen(PARENT, str(self.here)), "a", self.clock[0])
        raw = json.dumps({"session_id": PARENT, "cwd": str(self.here)}).encode()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a"),
                                             "FAKE_NO_LIST": "1"}):
            self.go("hook", "prompt")
        self.assertEqual(store.worker("w-1").cost, 2.5)


if __name__ == "__main__":
    unittest.main()
