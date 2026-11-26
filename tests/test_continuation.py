"""A resume that goes on under a new session id (another account is another organization: the
tool continues the chat as a copy) is linked to the session it continues: FAIRSHARE_CONTINUES
from the launcher, `store.link_session`, and the readers that follow `moved_to`. The fleet of
test_handover (accounts a, out; b, ranked first; c)."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import ROLE_WORKER, Request, SessionRecord, SessionSeen, Worker

from test_honest import NOW
from test_pickup import SID, TRANSCRIPT
from test_handover import Handover

NEW = "9f8e7d6c-0000-4000-8000-000000000002"
LAUNCHER = 7777


class Continuation(Handover):
    """The old session SID on a (stranded, opus at high effort, a worker of p-1, with costs and
    a checkpoint note); a worker w-1 of SID with its own session s-w."""

    def setUp(self):
        super().setUp()
        self.record(model="claude-opus-5-5", effort="high")
        store.note_session(SessionSeen(SID, cost=2.5), "a", NOW - 50)
        store.update_session(SID, lambda old: SessionRecord(
            **{**vars(old), "role": ROLE_WORKER, "parent": "p-1"}))
        store.add_worker(Worker("w-1", SID, "a", str(self.work), NOW - 600, "s-w"))
        store.update_session("s-w", lambda old: SessionRecord(
            "s-w", "a", str(self.work), None, NOW - 100, role=ROLE_WORKER, parent=SID))
        store.append_checkpoint(SID, "## old\nparser done\n")

    def env(self, value=f"{LAUNCHER}:{SID}"):
        return {"FAIRSHARE_CONTINUES": value} if value is not None else {}

    def stdin(self, payload):
        return mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(
            json.dumps(payload).encode())))

    def capture(self, sid=NEW, account="b", value=f"{LAUNCHER}:{SID}", ancestors=(LAUNCHER,),
                **extra):
        payload = {"session_id": sid, "cwd": str(self.work), "transcript_path": "/t/new.jsonl",
                   **extra}
        with self.stdin(payload), mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts" / account), **self.env(value)}), \
                mock.patch.object(cli, "_ancestors", return_value=set(ancestors)):
            return self.go("capture")

    def note(self, sid):
        return store.checkpoint_path(sid).read_text()


class Link(Continuation):
    def test_a_new_id_takes_the_old_records_place(self):
        self.capture(cost={"total_cost_usd": 0.5})
        new, old = store.session(NEW), store.session(SID)
        self.assertEqual((new.account, new.role, new.parent, new.model, new.model_name,
                          new.effort, new.continues, new.cwd, new.transcript),
                         ("b", ROLE_WORKER, "p-1", "claude-opus-5-5", "Claude-Opus-5-5", "high",
                          SID, str(self.work), "/t/new.jsonl"))
        self.assertEqual(store.total_cost(new.costs), 0.5)                # its own, not copied
        self.assertEqual((old.account, old.moved_to, old.stranded_at, old.waiting_until),
                         ("a", NEW, None, None))
        self.assertEqual(store.total_cost(old.costs), 2.5)                # kept where it was

    def test_a_model_the_new_session_names_wins(self):
        self.capture(model={"id": "claude-sonnet-5-5", "display_name": "Sonnet"},
                     effort={"level": "low"})
        new = store.session(NEW)
        self.assertEqual((new.model, new.model_name, new.effort),
                         ("claude-sonnet-5-5", "Sonnet", "low"))

    def test_workers_are_re_parented(self):
        self.capture()
        self.assertEqual([w.name for w in store.workers(NEW)], ["w-1"])
        self.assertEqual(store.workers(SID), [])
        self.assertEqual(store.session("s-w").parent, NEW)

    def test_the_note_is_copied_not_moved(self):
        self.capture()
        self.assertEqual(self.note(NEW), "## old\nparser done\n")
        self.assertEqual(self.note(SID), "## old\nparser done\n")

    def test_a_note_the_new_id_has_is_kept(self):
        store.append_checkpoint(NEW, "## new\nmine\n")
        self.capture()
        self.assertEqual(self.note(NEW), "## new\nmine\n")

    def hook(self, name, event):
        raw = {"session_id": NEW, "cwd": str(self.work), "hook_event_name": event,
               "transcript_path": str(TRANSCRIPT), "error": "rate_limit"}
        with self.stdin(raw), mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/b"), **self.env()}), \
                mock.patch.object(cli, "_ancestors", return_value={LAUNCHER}):
            self.assertEqual(self.go("hook", name)[0], 0)
        self.assertEqual((store.session(NEW).continues, store.session(SID).moved_to,
                          store.session(NEW).role), (SID, NEW, ROLE_WORKER))

    def test_the_prompt_hook_links(self):
        self.hook("prompt", "UserPromptSubmit")

    def test_the_stop_failure_hook_links(self):
        self.hook("stop-failure", "StopFailure")
        self.assertIsNotNone(store.session(NEW).stranded_at)


class NoLink(Continuation):
    def unlinked(self):
        new, old = store.session(NEW), store.session(SID)
        self.assertIsNone(old.moved_to)
        self.assertEqual(old.parent, "p-1")
        self.assertEqual(store.session("s-w").parent, SID)
        self.assertFalse(store.checkpoint_path(NEW).exists())
        return new

    def test_a_pid_that_is_not_an_ancestor(self):
        self.capture(ancestors=(1234,))
        new = self.unlinked()
        self.assertEqual((new.account, new.role, new.continues), ("b", None, None))

    def test_no_variable_or_a_malformed_one(self):
        for value in (None, "", SID, f"x:{SID}", f"{LAUNCHER}:", f"{LAUNCHER}:../x", "1:" + SID,
                      f"{LAUNCHER}:no-such-session"):
            with self.subTest(value=value):
                self.assertEqual(self.capture(value=value)[0], 0)
                self.assertIsNone(self.unlinked().continues)

    def test_the_same_id_only_records(self):
        self.capture(sid=SID)
        self.assertIsNone(self.unlinked())
        rec = store.session(SID)
        self.assertEqual((rec.account, rec.continues), ("b", None))   # the account written

    def test_already_linked_changes_nothing(self):
        self.record(sid="s-other", account="c")
        self.capture()
        before = {p.name: p.read_text() for p in (self.tmp / "state/sessions").glob("*.json")}
        self.assertIsNone(store.link_session(SID, SessionSeen(NEW), "b", NOW))
        self.capture(value=f"{LAUNCHER}:s-other")                       # another old id
        after = {p.name: p.read_text() for p in (self.tmp / "state/sessions").glob("*.json")}
        self.assertEqual(after, before)
        self.assertIsNone(store.session("s-other").moved_to)

    def test_an_old_record_that_moved_already_is_not_linked_again(self):
        self.capture()
        self.capture(sid="s-nested")                     # a nested session under the launcher
        self.assertIsNone(store.session("s-nested").continues)
        self.assertEqual(store.session(SID).moved_to, NEW)


class Launcher(Continuation):
    """The launcher passes FAIRSHARE_CONTINUES for a resume only."""

    def test_a_resume_names_the_session_it_continues(self):
        self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)])
        env = self.calls[1][1]
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}")
        self.assertEqual(store.session(SID).account, "a")                  # not moved ahead
        self.capture(sid=SID, value=env["FAIRSHARE_CONTINUES"], ancestors=(os.getpid(),))
        self.assertEqual(store.session(SID).account, "b")                  # the capture writes it

    def test_not_for_a_switch_and_an_inherited_value_is_dropped(self):
        with mock.patch.dict(os.environ, {"FAIRSHARE_CONTINUES": f"{LAUNCHER}:{SID}"}):
            self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, "switch")])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("FAIRSHARE_CONTINUES" not in env for _, env, _ in self.calls))
        self.capture(value=None)                       # the switch's fresh session
        self.assertIsNone(store.session(NEW).continues)
        self.assertEqual((store.session(SID).moved_to, store.session("s-w").parent), (None, SID))

    def test_a_foreground_pickup_from_a_terminal(self):
        self.go("pickup", SID[:8], tty=True)
        env = self.launched.call_args.args[1]
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}")


class Handoff(Continuation):
    """A handoff's fresh session continues the old one: linked in its place, as a resume under a
    new id is, and started in the folder the old session started in, though its shell moved."""

    def moved_shell(self):
        (wt := self.work / "wt").mkdir()
        store.note_session(SessionSeen(SID, str(wt)), "a", NOW - 40)     # a hook after `cd wt`
        return wt

    def handed_off(self):
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, "handoff")])
        self.assertEqual(code, 0, err)
        env = self.calls[1][1]
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}")
        self.capture(value=env["FAIRSHARE_CONTINUES"], ancestors=(os.getpid(),))

    def test_a_handoff_links_and_re_parents_workers(self):
        self.handed_off()
        new, old = store.session(NEW), store.session(SID)
        self.assertEqual((new.account, new.continues, new.role, new.parent), ("b", SID,
                                                                             ROLE_WORKER, "p-1"))
        self.assertEqual((old.account, old.moved_to), ("a", NEW))
        self.assertEqual([w.name for w in store.workers(NEW)], ["w-1"])
        self.assertEqual(store.session("s-w").parent, NEW)

    def test_watch_in_the_new_session_sees_the_old_ones_worker(self):
        self.handed_off()
        store.append_checkpoint("s-w", "## w\nhalf\n")
        code, out, err = self.inside("watch", "--timeout", "1", sid=NEW)
        self.assertEqual(code, 0, err)
        self.assertIn("worker w-1 wrote a checkpoint", out)

    def test_a_moved_shell_is_handed_off_and_picked_up_in_its_start_folder(self):
        self.moved_shell()
        self.assertEqual(store.session(SID).cwd, str(self.work))
        for kind in ("handoff", "pickup"):
            with self.subTest(kind=kind):
                self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, kind)])
                self.assertEqual(Path(self.calls[1][2]).resolve(), self.work.resolve())

    def test_without_a_launcher_the_background_session_is_linked_in_its_folder(self):
        self.moved_shell()
        code, out, err = self.inside("handoff", "Done A.")
        self.assertEqual(code, 0, err)
        self.assertEqual(Path((self.log / "cwd-b").read_text().strip()), self.work.resolve())
        fresh = "s-work-h2"
        self.assertEqual((store.session(fresh).continues, store.session(SID).moved_to),
                         (SID, fresh))
        self.assertEqual(store.session("s-w").parent, fresh)
        self.assertEqual([w.name for w in store.workers(fresh)], ["w-1"])


class Readers(Continuation):
    """After a link, the readers find the end of the chain."""

    def setUp(self):
        super().setUp()
        self.capture()

    def test_status_hides_the_moved_record(self):
        store.note_session(SessionSeen(SID, cost=4.5), "a", NOW)          # rose in the last hour
        code, out, err = self.go("status")
        self.assertEqual(code, 0, err)
        self.assertNotIn(f"session {SID[:8]}", out)
        self.assertIn(f"session {NEW[:8]}", out)
        self.assertEqual(store.spend(store.session(SID).costs, NOW - 3600, NOW), 2.0)   # kept

    def test_pickup_by_the_old_prefix_finds_the_new_id(self):
        self.assertEqual(cli._find_session(SID[:8]).id, NEW)
        self.assertEqual(cli._find_session(SID).id, NEW)

    def test_checkpoint_request_all_reaches_re_parented_workers(self):
        for here in (NEW, SID):
            with self.subTest(here=here):
                code, out, err = self.inside("checkpoint", "--request", "all", sid=here)
                self.assertEqual(code, 0, err)
                self.assertIn("w-1 (session s-w): asked for a checkpoint", out)
        code, out, _ = self.go("checkpoint", "--request", SID[:8])
        self.assertNotIn("matches", out)

    def test_watch_from_the_new_id_sees_the_worker(self):
        store.append_checkpoint("s-w", "## w\nhalf\n")
        code, out, err = self.inside("watch", "--timeout", "1", sid=NEW)
        self.assertEqual(code, 0, err)
        self.assertIn("worker w-1 wrote a checkpoint", out)
        store.append_checkpoint("s-w", "more\n")
        code, out, err = self.inside("watch", "--timeout", "1", sid=SID)   # the old id: the end
        self.assertIn("worker w-1 wrote a checkpoint", out)
        self.assertIsNone(store.session(SID).reported.get("checkpoint:s-w"))

    def test_a_cycle_or_a_long_chain_stops(self):
        store.update_session(NEW, lambda old: SessionRecord(**{**vars(old), "moved_to": SID}))
        self.assertIn(store.latest(SID), (SID, NEW))
        self.assertIn(store.latest(NEW), (SID, NEW))
        ids = [f"c-{i}" for i in range(20)]
        for i, sid in enumerate(ids):
            store.update_session(sid, lambda old, i=i: SessionRecord(
                ids[i], "a", None, None, NOW, moved_to=ids[i + 1] if i + 1 < len(ids) else None))
        self.assertEqual(store.latest("c-0"), f"c-{store.CHAIN_STEPS}")


class StillRunning(Continuation):
    """A session that may still run."""

    def setUp(self):
        super().setUp()
        store.update_session(SID, lambda old: SessionRecord(**{**vars(old), "stranded_at": None}))
        (self.log / "live").write_text(f"{SID}\n")                   # still running

    def test_a_pickup_of_it_from_no_terminal_is_refused_and_stops_nothing(self):
        code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 1)
        self.assertIn(f"{SID[:8]} is still running; not picked up", err)
        self.assertEqual((self.bg_args("b"), (self.log / "stops").exists()), ([], False))
        self.assertEqual(store.session(SID).account, "a")


class StoppedCopy(Continuation):
    """A stranded session the tool continued under a new id by itself really stopped: the copy
    takes its place, as a resume on another account does."""

    def test_a_background_resume_that_became_a_copy_moves(self):
        with mock.patch.dict(os.environ, {"FAKE_COPY": "1"}):
            code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        copy = "s-pickup-0a1b2c3d"
        self.assertNotIn("may still be running", err)
        self.assertEqual(store.session(SID).moved_to, copy)
        self.assertEqual(store.session("s-w").parent, copy)
        self.assertEqual([w.name for w in store.workers(copy)], ["w-1"])


if __name__ == "__main__":
    unittest.main()
