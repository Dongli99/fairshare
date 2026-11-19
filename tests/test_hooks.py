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

    def worker(self, sid=SID, requested=None):
        w = Worker("w-1", PARENT, "a", str(self.here), NOW)
        store.add_worker(w)
        cli._joined(w, sid, NOW)
        if requested:
            store.update_session(sid, lambda r: cli.replace(r, checkpoint_requested=requested))

    def test_nothing_to_say_prints_nothing(self):
        self.assertIsNone(self.hook())
        self.assertIsNone(self.hook("prompt"))
        self.assertEqual(store.session(SID).account, "a")        # the session is noted

    def test_the_output_shape_names_the_event(self):
        self.snap("a", five=(91, 3), seven=(10, 50))
        self.assertEqual(self.hook()["hookEventName"], "PostToolUse")
        self.snap("a", five=(96, 3), seven=(10, 50))
        self.assertEqual(self.hook("prompt")["hookEventName"], "UserPromptSubmit")

    def test_main_at_90_then_95_once_each(self):
        self.snap("a", five=(91, 3), seven=(10, 50))
        self.assertEqual(self.said(), (
            "fairshare: account a is at 91% of its 5h window (resets 11:00). Best other account: "
            "b (5h 10% · 7d 40%). If what is left of your work is small, carry on. Otherwise, at "
            'the next stopping point: run fairshare handoff "<what was done, what is next, open '
            'decisions, constraints the user set, what was tried and failed, files touched>" to '
            "continue in a fresh session on the best account, which reads your note first. "
            "Workers you started: none; to have them checkpoint: fairshare checkpoint "
            "--request all."))
        self.assertIsNone(self.said())
        self.assertIsNone(self.said("prompt"))                   # the other event shares it
        self.snap("a", five=(94, 3), seven=(10, 50))
        self.assertIsNone(self.said())
        self.snap("a", five=(95, 3), seven=(10, 50))
        self.assertIn("account a is at 95% of its 5h window", self.said())
        self.snap("a", five=(99, 3), seven=(10, 50))
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned, {"five_hour": [NOW + 3 * H, 95]})

    def test_warning_move_pickup_keeps_the_pickup_text(self):
        cfg = self.tmp / "accounts.toml"
        cfg.write_text(cfg.read_text().replace(
            "\n\n[[account]]", '\n[policy]\nwarning_move = "pickup"\n\n[[account]]', 1))
        self.snap("a", five=(91, 3), seven=(10, 50))
        self.assertEqual(self.said(), (
            "fairshare: account a is at 91% of its 5h window (resets 11:00). Best other account: "
            "b (5h 10% · 7d 40%). If what is left of your work is small, carry on. Otherwise, at "
            'the next stopping point: write a checkpoint (fairshare checkpoint "<where you '
            'stand>") and run fairshare pickup to continue on the best account. Workers you '
            "started: none; to have them checkpoint: fairshare checkpoint --request all."))

    def test_a_new_reset_warns_again(self):
        self.snap("a", five=(96, 3), seven=(10, 50))
        self.assertIsNotNone(self.said())
        self.snap("a", five=(92, 8), seven=(10, 50))             # the next five-hour window
        self.assertIn("at 92% of its 5h window (resets 16:00)", self.said())

    def test_a_jump_to_96_says_one_95_warning(self):
        self.snap("a", five=(96, 3), seven=(10, 50))
        self.assertIn("at 96% of its 5h window", self.said())
        self.assertIsNone(self.said())
        self.assertEqual(store.session(SID).warned["five_hour"][1], 95)

    def test_the_week_is_named_by_its_length_and_the_higher_crossing_wins(self):
        self.snap("a", five=(91, 3), seven=(96, 50))
        self.assertIn("at 96% of its 7d window (resets Sun 10:00)", self.said())
        self.assertIsNone(self.said())                           # the 5h crossing was covered

    def test_workers_and_none_can_take_work(self):
        store.add_worker(Worker("w-x", SID, "b", str(self.here), NOW))
        store.add_worker(Worker("w-y", SID, "b", str(self.here), NOW))
        for i in "bc":
            self.snap(i, five=(100, 3), seven=(10, 50))
        self.snap("a", five=(91, 3), seven=(10, 50))
        text = self.said()
        self.assertIn("Best other account: none can take work now.", text)
        self.assertIn("Workers you started: w-x, w-y;", text)

    def test_worker_warning(self):
        self.worker()
        self.snap("a", five=(10, 3), seven=(90, 50))
        self.assertEqual(self.said(), (
            "fairshare: this account is at 90% of its 7d window. You are a worker: finish your "
            'current step, then run fairshare checkpoint "<what is done, what is next>". Do not '
            "start a big new step; your main session decides what happens next."))

    def test_a_worker_hears_a_request_once(self):
        self.worker(requested=NOW)
        self.snap("a", five=(91, 3), seven=(10, 50))
        self.assertEqual(self.said(), cli.CHECKPOINT_ASK)
        self.assertEqual(cli.CHECKPOINT_ASK, "fairshare: your main session asks for a checkpoint "
                         'now: finish your current step and run fairshare checkpoint "<what is '
                         'done, what is next>".')
        self.assertIn("You are a worker", self.said())           # then the crossing
        self.assertIsNone(self.said())
        store.update_session(SID, lambda r: cli.replace(r, checkpoint_requested=NOW + 60))
        self.assertEqual(self.said("prompt"), cli.CHECKPOINT_ASK)  # a new request

    def test_a_main_session_hears_no_request(self):
        store.note_session(SessionSeen(SID), "a", NOW)
        store.update_session(SID, lambda r: cli.replace(r, checkpoint_requested=NOW))
        self.assertIsNone(self.said())

    def test_a_subagent_hears_nothing(self):
        self.worker(requested=NOW)
        self.snap("a", five=(96, 3), seven=(96, 50))
        for name in ("post-tool", "prompt"):
            self.assertIsNone(self.hook(name, agent_id="agent-1", agent_type="Explore"))
        self.assertEqual(store.session(SID).warned, {})          # nothing marked either
        self.assertIsNone(store.session(SID).request_said)

    def test_stale_numbers_say_nothing_and_start_a_probe(self):
        self.snap("a", five=(96, 3), seven=(10, 50), at=NOW - 11 * 60)   # 2 x 5 min ago
        self.assertIsNone(self.said())
        self.assertEqual(self.probes, ["a"])
        self.snap("a", five=(96, 3), seven=(10, 50), at=NOW - 9 * 60)
        self.assertIsNotNone(self.said())

    def test_outside_the_hub_or_without_a_session_nothing(self):
        self.snap("a", five=(96, 3), seven=(10, 50))
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(b'{"cwd": "/x"}'))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            self.assertEqual(self.go("hook", "post-tool"), (0, "", ""))
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(json.dumps(
                {"session_id": SID}).encode()))):
            self.assertEqual(self.go("hook", "prompt"), (0, "", ""))

    def test_the_prompt_hook_joins_a_pending_worker_by_name(self):
        store.add_worker(Worker("w-late", PARENT, "a", str(self.here), NOW))
        (self.log / "live").write_text("w-late\n")
        self.snap("a", five=(91, 3), seven=(10, 50))
        self.assertIn("You are a worker", self.said("prompt", sid="s-w-late"))
        self.assertEqual(store.worker("w-late").session, "s-w-late")

    def test_post_tool_never_lists_sessions(self):
        store.add_worker(Worker("w-late", PARENT, "a", str(self.here), NOW))
        with mock.patch.object(claude, "list_sessions") as listed:
            self.said("post-tool", sid="s-w-late")
        listed.assert_not_called()


class NewParent(Speaking):
    """A worker hears, once, that its main session continued under a new id."""
    NEW = "9f8e7d6c-0000-4000-8000-00000000000b"

    def handed_off(self, copy=False):
        store.note_session(SessionSeen(PARENT), "a", NOW)
        store.link_session(PARENT, SessionSeen(self.NEW), "b", NOW, copy=copy)

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

    def test_a_copy_or_a_same_id_pickup_says_nothing(self):
        self.worker()
        self.handed_off(copy=True)                               # the old one may still run
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
