"""Stage 5b, part C: what the PostToolUse and UserPromptSubmit hooks say. The fleet of
test_workers (temp HOME, config, state and hub; fakes first on PATH; inherited session
variables cleared). The session runs on account a."""
import io
import json
import os
import statistics
import subprocess
import sys
import time
from unittest import mock

from fairshare import cli, store
from fairshare.model import ROLE_WORKER, SessionSeen, Worker
from fairshare.providers import claude

from test_honest import H, NOW
from test_session import SRC
from test_workers import PARENT, Work

SID = "5e55105e-0000-4000-8000-000000000005"


class Speaking(Work):
    """Accounts a (ranks first), b and c (test_workers.Work), all on Pro: a main session on a
    reaches the handoff stage at 10 points of its 5h window left (decision 36)."""

    def setUp(self):
        super().setUp()
        self.probes = []
        p = mock.patch.object(cli, "_start_probe",
                              lambda acct, cwd, now: self.probes.append(acct.id))
        p.start()
        self.addCleanup(p.stop)

    def hook(self, name="post-tool", sid=SID, **extra):
        raw = {"session_id": sid, "cwd": str(self.here), "transcript_path": "/t/x.jsonl",
               "hook_event_name": "PostToolUse" if name == "post-tool" else "UserPromptSubmit",
               **extra}
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(
                json.dumps(raw).encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            code, out, err = self.go("hook", name)
        self.assertEqual((code, err), (0, ""))
        if not out:
            return None
        d = json.loads(out)
        self.assertEqual(list(d), ["hookSpecificOutput"])
        self.assertEqual(set(d["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
        return d["hookSpecificOutput"]

    def said(self, name="post-tool", sid=SID, **extra):
        got = self.hook(name, sid, **extra)
        return got and got["additionalContext"]

    def stage(self, five=(92, 3), seven=(10, 50), **kw):
        """Account a's windows; by default in its handoff stage: 8 points of its 5h left, the
        5h resetting in 3 hours."""
        self.snap("a", five=five, seven=seven, **kw)

    def worker(self, sid=SID, requested=None):
        w = Worker("w-1", PARENT, "a", str(self.here), NOW)
        store.add_worker(w)
        cli._joined(w, sid, NOW)
        if requested:
            store.update_session(sid, lambda r: cli.replace(r, checkpoint_requested=requested))

    HANDOFF = ('fairshare: account a is in its handoff stage: write your checkpoint and hand off '
               'now with fairshare handoff "<what is done, what is next, open decisions, files '
               'touched>"; it moves this session\'s workers on a and continues the work on b.')
    OVER = "fairshare: the handoff stage on account a is over: keep working here."

    def test_nothing_to_say_prints_nothing(self):
        self.assertIsNone(self.hook())
        self.assertIsNone(self.hook("prompt"))
        self.assertEqual(store.session(SID).account, "a")        # the session is noted

    def test_the_output_shape_names_the_event(self):
        self.stage()
        self.assertEqual(self.hook()["hookEventName"], "PostToolUse")
        self.stage(five=(92, 8))                                 # the next 5h window
        self.assertEqual(self.hook("prompt")["hookEventName"], "UserPromptSubmit")

    def test_continue_says_nothing(self):
        self.stage(five=(89, 3))                                 # 11 points left
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {})

    def test_once_per_instance_of_the_binding_window(self):
        self.stage()
        self.assertEqual(self.said(), self.HANDOFF)
        self.assertIsNone(self.said())
        self.assertIsNone(self.said("prompt"))                   # the other event shares it
        self.stage(five=(99, 3))                                 # nearer, same instance
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {"handoff": [NOW + 3 * H, "a", True]})
        self.stage(five=(95, 8))                                 # the next five-hour window
        self.assertEqual(self.said(), self.HANDOFF)

    def test_the_week_binds_then_the_short_window_says_it_again(self):
        self.stage(five=(50, 3), seven=(99, 50))                 # the week: 100/13 points left
        self.assertEqual(self.said(), self.HANDOFF)
        self.assertEqual(store.session(SID).warned, {"handoff": [NOW + 50 * H, "a", True]})
        self.assertIsNone(self.said())
        self.stage(five=(95, 3), seven=(99, 50))                 # now the 5h binds: 5 points
        self.assertEqual(self.said(), self.HANDOFF)

    def test_the_target_is_the_first_other_account_that_can_take_the_work(self):
        self.snap("b", five=(80, 3), seven=(40, 50))             # 20 points for 3 hours: no
        self.stage()
        self.assertTrue(self.said().endswith("continues the work on c."))

    def test_with_no_target_checkpoint_and_keep_working_into_the_limit(self):
        for i in "bc":
            self.snap(i, five=(100, 3), seven=(10, 50))
        self.stage()
        self.assertEqual(self.said(), (
            "fairshare: account a is in its handoff stage and no account can take the work now: "
            'write your checkpoint now (fairshare checkpoint "<what is done, what is next>"), '
            "then keep working; when the limit stops this session, Claude Code resumes it at "
            "11:00."))
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {"handoff": [NOW + 3 * H, "a", False]})

    def test_a_target_that_appears_later_in_the_same_instance_is_said_once(self):
        for i in "bc":
            self.snap(i, five=(100, 3), seven=(10, 50))
        self.stage()
        self.assertIn("no account can take the work now", self.said())
        self.assertIsNone(self.said())                           # still none: nothing new
        self.snap("c", five=(10, 4), seven=(10, 50))             # c's 5h reset
        self.assertEqual(self.said().splitlines()[0],          # (a worker's line may follow)
                         self.HANDOFF.replace("work on b.", "work on c."))
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned["handoff"], [NOW + 3 * H, "a", True])
        self.snap("c", five=(100, 3), seven=(10, 50))             # gone again: nothing to say
        self.assertIsNone(self.said())

    def test_the_all_clear_when_the_stage_ends_on_the_same_account(self):
        for label, after in (("a reset", dict(five=(92, -1))),
                             ("usage credited back", dict(five=(80, 3)))):
            with self.subTest(label):
                store.update_session(SID, lambda r: r and cli.replace(r, warned={}))
                self.stage()
                self.assertEqual(self.said(), self.HANDOFF)
                self.stage(**after)
                self.assertEqual(self.said(), self.OVER)
                self.assertIsNone(self.said())
                self.assertEqual(store.session(SID).warned, {})

    def test_a_limit_lifting_within_half_an_hour_ends_the_stage_saying_when(self):
        self.stage()
        self.assertEqual(self.said(), self.HANDOFF)
        self.stage(five=(92, 0.4))                               # as little room, lifts at 08:24
        self.assertEqual(self.said(), "fairshare: account a's limit lifts at 08:24: keep working "
                                      "here; if it stops this session first, Claude Code resumes "
                                      "it then.")
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {})

    def test_a_mark_for_another_account_is_dropped_silently(self):
        store.note_session(SessionSeen(SID), "a", NOW)
        store.update_session(SID, lambda r: cli.replace(r, warned={"handoff": [NOW + 3 * H, "b"]}))
        self.stage(five=(10, 3))
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {})

    def test_an_old_warning_mark_does_not_silence_the_stage(self):
        store.note_session(SessionSeen(SID), "a", NOW)
        store.update_session(SID, lambda r: cli.replace(r, warned={"five_hour": NOW + 3 * H}))
        self.stage()
        self.assertEqual(self.said(), self.HANDOFF)

    def test_a_worker_hears_no_usage_line(self):
        self.worker()
        self.stage(five=(99, 3), seven=(99, 50))
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {})

    def test_a_worker_hears_a_request_once(self):
        self.worker(requested=NOW)
        self.stage()
        self.assertEqual(self.said(), cli.CHECKPOINT_ASK)
        self.assertEqual(cli.CHECKPOINT_ASK, "fairshare: your main session asks for a checkpoint "
                         'now: finish your current step and run fairshare checkpoint "<what is '
                         'done, what is next>".')
        self.assertIsNone(self.said())
        store.update_session(SID, lambda r: cli.replace(r, checkpoint_requested=NOW + 60))
        self.assertEqual(self.said("prompt"), cli.CHECKPOINT_ASK)  # a new request

    def test_a_main_session_hears_no_request(self):
        store.note_session(SessionSeen(SID), "a", NOW)
        store.update_session(SID, lambda r: cli.replace(r, checkpoint_requested=NOW))
        self.assertIsNone(self.said())

    def test_a_subagent_hears_nothing(self):
        store.note_session(SessionSeen(SID), "a", NOW)
        self.stage()
        for name in ("post-tool", "prompt"):
            self.assertIsNone(self.hook(name, agent_id="agent-1", agent_type="Explore"))
        self.assertEqual(store.session(SID).warned, {})          # nothing marked either

    def test_stale_numbers_say_nothing_and_start_a_probe(self):
        self.stage(at=NOW - 11 * 60)                             # 2 x 5 min ago
        self.assertIsNone(self.said())
        self.assertEqual(self.probes, ["a"])
        self.stage(at=NOW - 9 * 60)
        self.assertEqual(self.said(), self.HANDOFF)

    def test_outside_the_hub_or_without_a_session_nothing(self):
        self.stage()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(b'{"cwd": "/x"}'))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            self.assertEqual(self.go("hook", "post-tool"), (0, "", ""))
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(json.dumps(
                {"session_id": SID}).encode()))):
            self.assertEqual(self.go("hook", "prompt"), (0, "", ""))

    def test_the_prompt_hook_joins_a_pending_worker_by_name(self):
        store.add_worker(Worker("w-late", PARENT, "a", str(self.here), NOW))
        (self.log / "live").write_text("w-late\n")
        self.stage()
        self.assertIsNone(self.said("prompt", sid="s-w-late"))   # a worker: nothing to say
        self.assertEqual(store.worker("w-late").session, "s-w-late")
        self.assertEqual(store.session("s-w-late").role, ROLE_WORKER)

    def test_post_tool_never_lists_sessions(self):
        store.add_worker(Worker("w-late", PARENT, "a", str(self.here), NOW))
        with mock.patch.object(claude, "list_sessions") as listed:
            self.said("post-tool", sid="s-w-late")
        listed.assert_not_called()


class WorkerStage(Speaking):
    """A main session hears of its workers' accounts on other accounts: the pickup that moves
    a worker when its account is in the handoff stage and a target exists, and when it ends."""
    WID = "s-w-b-0000-4000-8000"

    def setUp(self):
        super().setUp()
        store.note_session(SessionSeen(SID), "a", NOW)
        w = Worker("w-b", SID, "b", str(self.here), NOW)
        store.add_worker(w)
        cli._joined(w, self.WID, NOW)

    LINE = ("fairshare: account b, where worker w-b runs, is in its handoff stage: run fairshare "
            "pickup s-w-b-00 to continue the worker on a.")

    def test_the_pickup_line_once_then_the_all_clear(self):
        self.snap("b", five=(95, 3), seven=(40, 50))
        self.assertEqual(self.said(), self.LINE)
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {f"worker:{self.WID}": [NOW + 3 * H, "b"]})
        self.snap("b", five=(10, 4), seven=(40, 50))             # its 5h reset
        self.assertEqual(self.said(), "fairshare: the handoff stage on account b, where worker "
                                      "w-b runs, is over: no pickup needed.")
        self.assertIsNone(self.said())

    def test_a_limit_lifting_within_half_an_hour_says_when_and_no_pickup(self):
        self.snap("b", five=(95, 3), seven=(40, 50))
        self.assertEqual(self.said(), self.LINE)
        self.snap("b", five=(95, 0.4), seven=(40, 50))
        self.assertEqual(self.said(), "fairshare: account b's limit, where worker w-b runs, lifts "
                                      "at 08:24: no pickup needed.")
        self.assertIsNone(self.said())

    def test_the_main_accounts_line_comes_first(self):
        self.snap("b", five=(95, 3), seven=(40, 50))
        self.stage()
        self.assertEqual(self.said(), self.HANDOFF.replace("work on b.", "work on c.") + "\n"
                         + self.LINE.replace("worker on a.", "worker on c."))

    def test_no_target_nothing(self):
        self.snap("b", five=(95, 3), seven=(40, 50))
        self.stage(five=(75, 3))                                 # 25 points for 3 hours: no
        self.snap("c", five=(100, 3), seven=(10, 50))
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {})

    def test_a_worker_stopped_by_its_limit_ended_removed_or_on_the_main_account_is_not_said(self):
        self.snap("b", five=(95, 3), seven=(40, 50))
        cases = {
            "stopped": lambda: store.note_session(SessionSeen(self.WID), "b", NOW, stranded=True),
            "ended": lambda: store.update_session(SID, lambda r: cli.replace(
                r, reported={f"ended:{self.WID}": 1})),
            "removed": lambda: store.remove_worker("w-b", NOW),
            "on a": lambda: store.move_session(self.WID, "a")}
        for label, make in cases.items():
            with self.subTest(label):
                self.setUp()
                self.snap("b", five=(95, 3), seven=(40, 50))
                make()
                self.assertIsNone(self.said())


class NewParent(Speaking):
    """A worker hears, once, that its main session continued under a new id."""
    NEW = "9f8e7d6c-0000-4000-8000-00000000000b"

    def handed_off(self):
        store.note_session(SessionSeen(PARENT), "a", NOW)
        store.link_session(PARENT, SessionSeen(self.NEW), "b", NOW)

    def notice(self, name=None):
        return (f"fairshare: your main session is now {self.NEW}"
                + (f" ({name})" if name else "") + f": it continued {PARENT}. Reach it there.")

    def test_a_handoff_is_said_once_with_the_name(self):
        self.worker()
        self.assertIsNone(self.said())                           # told its parent at start
        self.handed_off()
        listed = [claude.LiveSession("other", "x", False), claude.LiveSession(self.NEW, "x-h2", True)]
        seen = []
        with mock.patch.object(claude, "list_sessions",
                               lambda acct: seen.append(acct.id) or listed):
            self.assertEqual(self.said(), self.notice("x-h2"))
            self.assertIsNone(self.said())
            self.assertIsNone(self.said("prompt"))
        self.assertEqual(seen, ["b"])                            # the new parent's account, once
        self.assertEqual(store.session(SID).parent_said, self.NEW)

    def test_unlisted_or_failing_listing_says_the_id(self):
        for listing in (lambda acct: [], lambda acct: None, mock.Mock(side_effect=OSError("x"))):
            with self.subTest(listing=listing):
                self.reset()
                self.worker()
                self.handed_off()
                with mock.patch.object(claude, "list_sessions", listing):
                    self.assertEqual(self.said(), self.notice())

    def reset(self):
        for p in (store.state_dir() / "sessions").glob("*.json"):
            p.unlink()
        for p in (store.state_dir() / "workers").glob("*.json"):
            p.unlink()

    def test_a_same_id_pickup_says_nothing(self):
        self.worker()
        store.note_session(SessionSeen(PARENT), "a", NOW)
        store.move_session(PARENT, "b")                          # same id, another account
        with mock.patch.object(claude, "list_sessions") as listed:
            self.assertIsNone(self.said())
        listed.assert_not_called()

    def test_an_old_record_without_the_field_says_nothing_until_reparented(self):
        self.worker()
        path = store.state_dir() / "sessions" / f"{SID}.json"
        d = json.loads(path.read_text())
        del d["parent_said"]
        path.write_text(json.dumps(d))
        self.assertIsNone(self.said())
        self.handed_off()
        with mock.patch.object(claude, "list_sessions", lambda acct: None):
            self.assertEqual(self.said(), self.notice())

    def test_a_request_and_the_notice_together(self):
        self.worker(requested=NOW)
        self.handed_off()
        with mock.patch.object(claude, "list_sessions", lambda acct: None):
            self.assertEqual(self.said(), self.notice() + "\n" + cli.CHECKPOINT_ASK)
            self.assertIsNone(self.said())


class TurnEnd(Speaking):
    """`hook stop`: a worker's turn end, stamped on its record for `watch`."""

    def stop(self, sid=SID, **extra):
        return self.hook("stop", sid, hook_event_name="Stop", stop_hook_active=False,
                         last_assistant_message="done", **extra)

    def test_a_worker_is_stamped_with_its_background_tasks(self):
        self.worker()
        self.assertIsNone(self.stop(background_tasks=[]))
        self.assertEqual(store.session(SID).stopped, [NOW, 0])
        self.clock[0] += 60
        self.stop(background_tasks=[{"id": "b1", "type": "shell", "status": "running"},
                                    {"id": "b2", "type": "monitor", "status": "running"}])
        self.assertEqual(store.session(SID).stopped, [NOW + 60, 2])
        self.clock[0] += 60
        self.stop()                                     # an input without the list: none
        self.assertEqual(store.session(SID).stopped, [NOW + 120, 0])

    def test_a_main_session_and_a_subagent_are_not_stamped(self):
        store.note_session(SessionSeen(SID), "a", NOW)
        self.stop()
        self.assertEqual(store.session(SID).stopped, [])
        self.assertIsNone(store.session("s-none"))
        self.stop(sid="s-none")                         # no record: none is made
        self.assertIsNone(store.session("s-none"))
        self.worker()
        self.stop(agent_id="agent-1", agent_type="Explore")
        self.assertEqual(store.session(SID).stopped, [])

    def test_a_pending_worker_is_joined_by_name_then_stamped(self):
        store.add_worker(Worker("w-late", PARENT, "a", str(self.here), NOW))
        (self.log / "live").write_text("w-late\n")
        self.stop(sid="s-w-late")
        self.assertEqual(store.session("s-w-late").stopped, [NOW, 0])


class Wired(Work):
    THEIRS = ('{\n  "hooks": {\n    "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": '
              '"command", "command": "~/lint.sh"}]}],\n    "UserPromptSubmit": [{"hooks": '
              '[{"type": "command", "command": "~/remind.sh"}]}]\n  }\n}\n')

    def test_ours_are_added_beside_the_users_own_hooks(self):
        new = claude.wire(self.THEIRS, "/bin/fairshare")
        d = json.loads(new)
        self.assertEqual(d["hooks"]["PostToolUse"], [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/lint.sh"}]},
            {"matcher": "*", "hooks": [{"type": "command",
                                        "command": "/bin/fairshare hook post-tool"}]}])
        self.assertEqual(d["hooks"]["UserPromptSubmit"], [
            {"hooks": [{"type": "command", "command": "~/remind.sh"}]},
            {"hooks": [{"type": "command", "command": "/bin/fairshare hook prompt"}]}])
        theirs_kept = (new.replace(',{"matcher": "*", "hooks": [{"type": "command", "command": '
                       '"/bin/fairshare hook post-tool"}]}', "")
                       .replace(',{"hooks": [{"type": "command", "command": "/bin/fairshare hook '
                                'prompt"}]}', ""))
        self.assertTrue(theirs_kept.startswith(self.THEIRS[:self.THEIRS.index("]\n  }\n}") + 1]))
        self.assertIsNone(claude.wire(new, "/bin/fairshare"))                 # idempotent
        self.assertEqual(claude.hooked(new), ("/bin/fairshare",))

    def test_a_matcher_on_our_prompt_group_is_left_and_still_recognised(self):
        new = claude.wire(None, "/bin/fairshare").replace(
            '"hooks": [\n          {\n            "type": "command",\n            "command": '
            '"/bin/fairshare hook prompt"', '"matcher": "x", "hooks": [\n          {\n            '
            '"type": "command",\n            "command": "/bin/fairshare hook prompt"')
        self.assertIn('"matcher": "x"', new)
        self.assertEqual(claude.hooked(new), ("/bin/fairshare",))
        self.assertIsNone(claude.wire(new, "/bin/fairshare"))

    def test_a_missing_one_fails_hooked_and_is_added(self):
        full = json.loads(claude.wire(None, "/bin/fairshare"))
        del full["hooks"]["PostToolUse"]
        text = json.dumps(full, indent=2)
        self.assertIsNone(claude.hooked(text))
        self.assertEqual(claude.hooked(claude.wire(text, "/bin/fairshare")), ("/bin/fairshare",))

    def test_an_install_with_the_three_older_hooks_is_flagged_and_only_stop_is_added(self):
        full = json.loads(claude.wire(None, "/bin/fairshare"))
        del full["hooks"]["Stop"]
        text = json.dumps(full, indent=2)
        self.assertIsNone(claude.hooked(text))          # doctor: FAIL, fix with --fix
        new = json.loads(claude.wire(text, "/bin/fairshare"))
        self.assertEqual(new["hooks"]["Stop"], [{"hooks": [
            {"type": "command", "command": "/bin/fairshare hook stop"}]}])
        self.assertEqual({k: v for k, v in new["hooks"].items() if k != "Stop"}, full["hooks"])


HOOK_RUNS = 20


class HookCost(Speaking):
    """The hook's wall time with nothing to say, as the provider runs it: a fresh process."""

    def test_median_wall_time(self):
        store.note_session(SessionSeen(SID, str(self.here)), "a", time.time())
        store.save("a", store.Snapshot(time.time(), {}))
        raw = json.dumps({"session_id": SID, "cwd": str(self.here),
                          "hook_event_name": "PostToolUse"}).encode()
        env = {**os.environ, "PYTHONPATH": SRC, "CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}
        times = []
        for _ in range(HOOK_RUNS):
            t = time.perf_counter()
            r = subprocess.run([sys.executable, "-P", "-m", "fairshare.cli", "hook", "post-tool"],
                               input=raw, env=env, capture_output=True, timeout=30)
            times.append(time.perf_counter() - t)
            self.assertEqual((r.returncode, r.stdout), (0, b""))
        median = statistics.median(times)
        print(f"\nhook post-tool, nothing to say: median {median * 1000:.0f} ms of {HOOK_RUNS}",
              file=sys.stderr)
        self.assertLess(median, 1.0)        # loose: the suite runs on loaded machines too
