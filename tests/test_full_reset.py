"""`fairshare full-reset`: the user plans a full reset the provider granted (fairshare cannot see
held resets). The planned account ranks with its week refilled until the reset is seen used (a
probe's fall, or a moved reset) or the week resets; the bar and the warning say so."""
import os
import tempfile
import unittest
from unittest import mock

from fairshare import cli, store
from fairshare.model import Account, Snapshot, Tuning, Window, load_plan

from test_honest import H, NOW, ORG, Fleet
import test_hooks
from test_rates import rated, spending
from test_workers import Work

WEEK = NOW + 50 * H                                  # Sun 10:00 UTC


class Command(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.snap("a", five=(10, 3), seven=(50, 50))
        self.snap("b", five=(10, 3), seven=(80, 50))

    def test_plan_list_and_cancel(self):
        self.assertEqual(self.go("full-reset", "b"), (0, (
            "full reset planned on b: +100% until it is used or the week resets (Sun 10:00); "
            "`fairshare full-reset b --cancel` drops it\n"), ""))
        self.assertEqual(store.active_full_resets(NOW), {"b": WEEK})
        self.assertEqual(self.go("full-reset")[1], "full reset planned on b until Sun 10:00\n")
        self.assertEqual(self.go("full-reset", "b", "--cancel")[1], "no full reset planned on b\n")
        self.assertEqual(self.go("full-reset")[1], "no full reset planned\n")

    def test_planning_again_replaces_the_plan_and_cancel_alone_drops_every_plan(self):
        self.go("full-reset", "a")
        store.record("a", {"seven_day": Window(10, WEEK)}, NOW + 60, probe=True)   # used
        self.assertEqual(store.active_full_resets(NOW + 60), {})
        self.go("full-reset", "a")
        self.go("full-reset", "b")
        self.assertEqual(store.full_resets(), {"a": (WEEK, None), "b": (WEEK, None)})
        self.assertEqual(self.go("full-reset", "--cancel")[1], "no full reset planned\n")
        self.assertEqual(store.full_resets(), {})

    def test_an_unknown_account_or_one_with_no_current_week_is_refused(self):
        code, out, err = self.go("full-reset", "nope")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "fairshare: no account 'nope' (configured: a, b)\n")
        self.add("c", orgId=ORG + "c")                               # no snapshot at all
        self.snap("b", five=(10, 3), seven=(80, -1))                 # its week already reset
        for i in ("c", "b"):
            with self.subTest(account=i):
                code, out, err = self.go("full-reset", i)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, f"fairshare: no current weekly window known for {i}, so "
                                      "nothing to plan against (`fairshare status` asks for its "
                                      "usage)\n")
        self.assertEqual(store.full_resets(), {})

    def test_a_plan_ends_at_the_weekly_reset(self):
        self.go("full-reset", "a")
        self.assertEqual(store.active_full_resets(WEEK - 1), {"a": WEEK})
        self.assertEqual(store.active_full_resets(WEEK), {})
        self.clock[0] = WEEK + 1
        self.assertEqual(self.go("full-reset")[1], "no full reset planned\n")

    def test_the_planned_account_ranks_first_and_status_says_so(self):
        out = self.go("status")[1]
        self.assertTrue(out.startswith("> a "), out)                 # 50% left beats 20%
        self.go("full-reset", "b")
        out = self.go("status")[1].splitlines()
        self.assertTrue(out[0].startswith("> b "), out)              # 120% beats 50%
        self.assertIn("seven_day 80% used, resets in 50h, +100% planned", out[0])
        self.assertEqual(out[1], "    full reset planned: +100% until it is used or the week "
                                 "resets (Sun 10:00); `fairshare full-reset b --cancel` drops it")
        self.assertNotIn("full reset", "\n".join(l for l in out if " a " in l))
        self.assertTrue(out[-1].startswith("why b: b before a (score "), out)
        self.assertIn("+100% planned (20% left until it is used); a: ", out[-1])
        self.go("run")
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/b"))


class Detection(unittest.TestCase):
    """The plan is marked used when `record` takes a lower value under the same reset (only the
    probe's credited-back path does) or a running window's reset moves."""
    OLD = {"five_hour": Window(60, 10_000), "seven_day": Window(70, 500_000)}

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = mock.patch.dict(os.environ, {"FAIRSHARE_STATE": d.name})
        env.start()
        self.addCleanup(env.stop)
        store.save("a", Snapshot(100.0, self.OLD))
        store.save("b", Snapshot(100.0, self.OLD))
        store.plan_full_reset("a", 500_000)
        store.plan_full_reset("b", 500_000)

    def used(self):
        return {k: used for k, (_, used) in store.full_resets().items()}

    def test_a_probes_fall_of_the_week_or_the_short_window_marks_it_used(self):
        for k in ("seven_day", "five_hour"):
            with self.subTest(window=k):
                self.setUp()
                store.record("a", {k: Window(3, self.OLD[k].resets_at - 60)}, 200.0, probe=True)
                self.assertEqual(self.used(), {"a": 200.0, "b": None})
                self.assertEqual(store.active_full_resets(200.0), {"b": 500_000})

    def test_a_status_line_redraw_lower_is_stale_and_marks_nothing(self):
        store.record("a", {"seven_day": Window(3, 500_000), "five_hour": Window(0, 10_000)}, 200.0)
        self.assertEqual(store.load("a").windows, self.OLD)
        self.assertEqual(self.used(), {"a": None, "b": None})

    def test_a_rise_or_a_probes_rounding_marks_nothing(self):
        store.record("a", {"seven_day": Window(75, 500_000)}, 200.0)
        store.record("a", {"seven_day": Window(74, 500_000)}, 300.0, probe=True)
        self.assertEqual(self.used(), {"a": None, "b": None})

    def test_a_moved_weekly_reset_ends_it(self):
        store.record("a", {"seven_day": Window(72, 500_000 + 2 * 86_400)}, 200.0)
        self.assertEqual(self.used(), {"a": 200.0, "b": None})

    def test_a_short_window_that_rolled_over_marks_nothing(self):
        store.record("a", {"five_hour": Window(2, 30_000)}, 10_500.0)
        self.assertEqual(self.used(), {"a": None, "b": None})

    def test_the_next_short_window_seen_just_before_the_stored_reset_marks_nothing(self):
        # the stored reset is to the minute: the new window can show up a little before it
        store.record("a", {"five_hour": Window(6, 9_950 + 18_000)}, 9_950.0, probe=True)
        self.assertEqual(self.used(), {"a": None, "b": None})

    def test_a_used_plan_is_not_marked_again(self):
        store.record("a", {"seven_day": Window(3, 500_000)}, 200.0, probe=True)
        store.record("a", {"five_hour": Window(1, 10_000)}, 300.0, probe=True)
        self.assertEqual(self.used()["a"], 200.0)


class Bar(Fleet):
    """Account a's rates (test_rates.rated): $1.00 a point of its week."""

    def line(self, week, plan=True, spent=4.0, rate=True):
        acct = Account("a", "claude", load_plan("claude-pro"), None)
        if plan:
            store.plan_full_reset("a", WEEK)
        if rate:
            rated(self.tmp, "a")
        else:
            (self.tmp / "state" / "usage" / "a.jsonl").unlink(missing_ok=True)
        if spent:
            spending("s-a", "a", spent)
        snap = Snapshot(NOW, {"five_hour": Window(10, NOW + 3 * H), "seven_day": Window(week, WEEK)})
        return cli._status_line(acct, snap, NOW, True, Tuning())

    def test_the_week_says_reset_then_use_reset_once_the_reset_is_due(self):
        self.assertEqual(self.line(20), "a · 5h 10% · 7d 20% +reset")
        self.assertEqual(self.line(92), f"a · 5h 10% · {cli.TIGHT}7d 92% (Sun 10:00) +reset"
                                        f"{cli.PLAIN}")               # $8 left: 2 h at $4.00
        self.assertEqual(self.line(97), f"a · 5h 10% · {cli.TIGHT}7d 97% (Sun 10:00) use reset"
                                        f"{cli.PLAIN}")               # $3 left: 45 min

    def test_no_spend_or_no_rate_is_not_due(self):
        for kw in ({"spent": 0}, {"rate": False}):
            with self.subTest(**kw):
                self.assertEqual(self.line(97, **kw), f"a · 5h 10% · {cli.TIGHT}7d 97% "
                                                      f"(Sun 10:00) +reset{cli.PLAIN}")

    def test_no_plan_no_marker(self):
        self.assertEqual(self.line(20, plan=False), "a · 5h 10% · 7d 20%")
        self.assertEqual(self.line(97, plan=False),
                         f"a · 5h 10% · {cli.TIGHT}7d 97% (Sun 10:00){cli.PLAIN}")


class Warning(Work):
    """The hooks of test_hooks (its helpers, not its tests); the session runs on account a, at
    $2.00 an hour; a's rates: $0.10 a point of its 5h window, $1.00 of its week."""
    hook, said, near = test_hooks.Speaking.hook, test_hooks.Speaking.said, test_hooks.Speaking.near

    def setUp(self):
        super().setUp()
        rated(self.tmp, "a")
        p = mock.patch.object(cli, "_start_probe", lambda acct, cwd, now: None)
        p.start()
        self.addCleanup(p.stop)

    CARRY_ON = ("The user planned a full reset on a and uses it once the limit stops work, so "
                "this is not a stop: keep working on this account until the limit stops you; the "
                "user then refills it and the stopped sessions continue here. No checkpoint, "
                "handoff or pickup.")

    def test_without_a_plan_the_usual_move(self):
        self.near(five=(10, 3), seven=(99, 50))
        said = self.said()
        self.assertIn("Best account to move to: b", said)
        self.assertNotIn("full reset", said)

    def test_a_week_near_its_limit_with_a_plan_says_work_to_the_limit(self):
        self.near(five=(10, 3), seven=(99, 50))
        store.plan_full_reset("a", NOW + 50 * H)
        said = self.said()
        self.assertEqual(said, "fairshare: account a has about $1.00 left before its limit, about "
                         "30 min at its pace in the last hour ($2.00); it refills at Sun 10:00 to "
                         "about $10.00. " + self.CARRY_ON)

    def test_a_short_window_binding_on_a_nearly_spent_week_says_the_same(self):
        self.near(five=(95, 3), seven=(98.5, 50))                   # 5h $0.50, week $1.50
        store.plan_full_reset("a", NOW + 50 * H)
        self.assertIn("refills at 11:00 to about $1.50. " + self.CARRY_ON, self.said())

    def test_a_worker_works_to_the_limit_too(self):
        test_hooks.Speaking.worker(self)
        self.near(five=(10, 3), seven=(99, 50))
        store.plan_full_reset("a", NOW + 50 * H)
        said = self.said()
        self.assertTrue(said.endswith(self.CARRY_ON), said)
        self.assertNotIn("checkpoint \"", said)

    def test_a_week_with_room_keeps_the_reset_and_moves(self):
        self.near(five=(92, 3), seven=(10, 50))                     # week $90 left
        store.plan_full_reset("a", NOW + 50 * H)
        said = self.said()
        self.assertNotIn("full reset", said)
        self.assertIn("refills at 11:00 to about $10.00. This account can no longer take work", said)
        self.assertIn("Best account to move to: b ", said)

    def test_a_planned_reset_elsewhere_makes_that_account_the_best_other(self):
        self.near()
        store.plan_full_reset("c", NOW + 50 * H)                    # c: 7d 50%, now 150% room
        said = self.said()
        self.assertIn("Best account to move to: c (5h 10% · 7d 50%)", said)
        self.assertNotIn("planned a full reset", said)


if __name__ == "__main__":
    unittest.main()
