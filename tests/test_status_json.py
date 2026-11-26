"""`fairshare status --json`: one JSON object on stdout, schema 3, every key always present.
Runs on the fleets of test_honest (temp HOME, config, state and hub; fakes first on PATH; the
clock injected). Made-up accounts and numbers throughout."""
import json
import unittest

from fairshare import store
from fairshare.model import Snapshot, Window

from test_honest import H, NOW, ORG, Fleet
from test_spend import costed

ACCOUNT_KEYS = {"id", "plan", "state", "rank", "eligible", "score", "usable_in_hours", "reason",
                "stage", "left_out", "same_login_as", "windows", "usage_as_of", "sign_in_ends",
                "notes", "sessions", "sessions_more"}
SESSION_KEYS = {"id", "model", "folder", "role"}


class StatusJson(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.add("a2", orgId=ORG + "a")                  # the same login as a
        self.add("out", auth=None)                       # sign-in check fails, never confirmed
        self.snap("a", five=(40, 3), seven=(50, 50))
        self.snap("b", five=(10, 3), seven=(20, 50))

    def status(self):
        code, out, err = self.go("status", "--json")
        self.assertEqual((code, err), (0, ""))
        return json.loads(out), out

    def by_id(self, data):
        return {a["id"]: a for a in data["accounts"]}

    def test_stdout_is_one_json_object_and_nothing_else(self):
        self.go("prefer", "b", "--for", "3h")
        self.snap("b", seven=(99, 100))                  # the preference gives way: a note in text
        code, out, err = self.go("status", "--json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)                           # the whole of stdout parses
        self.assertTrue(out.startswith("{\n  \"schema\": 3,\n  \"now\": "), out)   # indent=2
        self.assertTrue(out.endswith("}\n"))
        self.assertEqual(list(data), ["schema", "now", "top", "why", "preference", "accounts"])
        self.assertEqual((data["schema"], data["now"]), (3, NOW))

    def test_accounts_in_order_with_every_key_each(self):
        data, _ = self.status()
        self.assertEqual([(a["id"], a["state"]) for a in data["accounts"]],
                         [("b", "ranked"), ("a", "ranked"), ("out", "left_out"),
                          ("a2", "same_login")])
        states = [a["state"] for a in data["accounts"]]
        self.assertEqual(sorted(set(states)), ["left_out", "ranked", "same_login"])
        self.assertEqual(states, sorted(states, key={"ranked": 0, "left_out": 1,
                                                     "same_login": 2}.get))
        for a in data["accounts"]:
            self.assertEqual(set(a), ACCOUNT_KEYS, a["id"])

    def test_ranked_account(self):
        data, _ = self.status()
        accts = self.by_id(data)
        a, b = accts["a"], accts["b"]
        self.assertEqual(data["top"], "b")               # b has more room left
        self.assertEqual((b["rank"], a["rank"]), (1, 2))
        self.assertEqual((b["plan"], b["state"], b["eligible"]), ("claude-pro", "ranked", True))
        self.assertIsInstance(b["score"], float)
        self.assertEqual(b["usable_in_hours"], 0)
        self.assertIn("seven_day 20% used", b["reason"])
        self.assertEqual((b["stage"], b["left_out"], b["same_login_as"], b["sessions"],
                          b["sessions_more"]), ("continue", None, None, [], 0))
        self.assertEqual(b["notes"], ["usage probe failed for b (no answer: exit 1); using its "
                                      "snapshot from 08:00"])         # the fake serves no usage
        self.assertEqual(b["windows"], {"five_hour": {"used_pct": 10, "resets_at": NOW + 3 * H},
                                        "seven_day": {"used_pct": 20, "resets_at": NOW + 50 * H}})
        self.assertEqual(b["usage_as_of"], NOW)

    def test_left_out_account_has_nulls_for_the_ranking(self):
        a = self.by_id(self.status()[0])["out"]
        self.assertEqual((a["state"], a["rank"], a["eligible"], a["score"], a["usable_in_hours"],
                          a["reason"], a["same_login_as"], a["windows"], a["usage_as_of"]),
                         ("left_out", None, None, None, None, None, None, None, None))
        self.assertIn("never confirmed as a subscription login", a["left_out"])
        self.assertEqual(a["plan"], "claude-pro")

    def test_same_login_account_names_the_one_ranked(self):
        a = self.by_id(self.status()[0])["a2"]
        self.assertEqual((a["state"], a["same_login_as"], a["rank"], a["eligible"], a["score"],
                          a["usable_in_hours"], a["reason"], a["left_out"]),
                         ("same_login", "a", None, None, None, None, None, None))

    def test_why_is_what_text_prints_and_null_when_the_top_cannot_work(self):
        data, _ = self.status()
        text = self.go("status")[1].splitlines()[-1]
        self.assertEqual("why b: " + data["why"], text)
        self.assertTrue(data["why"].startswith("b before a ("))
        self.snap("a", seven=(99, 100))
        self.snap("b", seven=(99, 100))
        data, _ = self.status()
        self.assertEqual((data["why"], data["top"] in ("a", "b")), (None, True))
        self.assertFalse(self.by_id(data)["b"]["eligible"])
        self.assertGreater(self.by_id(data)["b"]["usable_in_hours"], 0)

    def test_preference_honoured_and_not(self):
        self.assertIsNone(self.status()[0]["preference"])
        self.go("prefer", "a", "--for", "3h")
        data, out = self.status()
        self.assertEqual(data["preference"], {"account": "a", "until": NOW + 3 * H,
                                              "honoured": True})
        self.assertEqual(data["top"], "a")
        self.snap("a", seven=(99, 100))                  # it cannot take work: the best is used
        data, _ = self.status()
        self.assertEqual(data["preference"], {"account": "a", "until": NOW + 3 * H,
                                              "honoured": False})
        self.assertEqual(data["top"], "b")

    def test_per_model_week_appears_under_its_key(self):
        store.save("b", Snapshot(NOW, {"five_hour": Window(10, NOW + 3 * H),
                                       "seven_day": Window(20, NOW + 50 * H),
                                       "seven_day:Fable": Window(99, NOW + 40 * H)}))
        b = self.by_id(self.status()[0])["b"]
        self.assertEqual(b["windows"]["seven_day:Fable"],
                         {"used_pct": 99, "resets_at": NOW + 40 * H})
        self.assertIn("warning: b's Fable week is spent until Sun 00:00; other models are "
                      "unaffected", b["notes"])

    def test_sessions_most_recently_seen_first_with_no_dollars(self):
        costed("s-main-1", "b", [0.5, 0.5, 1.0], seen=NOW - 120)
        costed("s-wrk-22", "b", [3.0, 3.0], cwd="/w/other", model=None, seen=NOW - 60)
        costed("s-quiet3", "b", [], model="Haiku", cwd=None, seen=NOW - 180)
        costed("s-gone-44", "b", [9.0], seen=NOW - 11 * 60)                # not live
        b = self.by_id(self.status()[0])["b"]
        self.assertEqual([s["id"] for s in b["sessions"]], ["s-wrk-22", "s-main-1", "s-quiet3"])
        for s in b["sessions"]:
            self.assertEqual(set(s), SESSION_KEYS)
        wrk, main, quiet = b["sessions"]
        self.assertEqual((wrk["model"], wrk["folder"], wrk["role"]), (None, "other", "main"))
        self.assertEqual((main["folder"], main["model"]), ("proj", "Opus"))
        self.assertEqual((quiet["folder"], quiet["model"]), (None, "Haiku"))
        self.assertEqual(b["sessions_more"], 0)
        text = self.go("status")[1]
        self.assertIn("    session s-wrk-22 · - · other · main\n", text)
        self.assertIn("    session s-quiet3 · Haiku · - · main\n", text)
        self.assertNotIn("$", text)

    def test_the_stage_per_account_in_json_and_text(self):
        self.snap("a", five=(92, 3), seven=(50, 50))                       # 8 points left
        data, _ = self.status()
        self.assertEqual({a["id"]: a["stage"] for a in data["accounts"]},
                         {"a": "handoff", "b": "continue", "out": None, "a2": None})
        text = self.go("status")[1]
        self.assertIn("five_hour 92% used, resets in 3.0h · handoff", text)
        self.assertIn("seven_day 20% used, resets in 50h · continue", text)

    def test_more_than_five_sessions_are_counted_not_listed(self):
        for i in range(7):
            costed(f"s-many-{i}", "a", [float(i + 1)], seen=NOW - (6 - i) * 60)
        a = self.by_id(self.status()[0])["a"]
        self.assertEqual((len(a["sessions"]), a["sessions_more"]), (5, 2))
        self.assertEqual(a["sessions"][0]["id"], "s-many-6")
        self.assertIn("    +2 more\n", self.go("status")[1])

    def test_sign_in_end_and_usage_age(self):
        accts = self.by_id(self.status()[0])
        self.assertIsInstance(accts["a"]["sign_in_ends"], float)   # the fixture's login has one
        self.assertIsNone(accts["out"]["sign_in_ends"])
        self.snap("a", seven=(50, 50), at=NOW - 2 * H)
        self.assertEqual(self.by_id(self.status()[0])["a"]["usage_as_of"], NOW - 2 * H)


class NothingRanked(Fleet):
    def test_no_account_can_take_work_so_there_is_no_top(self):
        self.add("out", auth=None)
        code, out, err = self.go("status", "--json")
        data = json.loads(out)
        self.assertEqual((code, err, data["top"], data["why"], data["preference"]),
                         (0, "", None, None, None))
        self.assertEqual([(a["id"], a["state"]) for a in data["accounts"]],
                         [("out", "left_out")])


if __name__ == "__main__":
    unittest.main()
