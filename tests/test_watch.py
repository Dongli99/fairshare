"""Stage 5b, part D: `fairshare watch`. The fleet of test_workers (temp HOME, config, state and
hub; fakes first on PATH; inherited session variables cleared); the clock and the sleep are
injected (Fleet), so the cadence is read off the clock. The watching session runs on a."""
import io
import json
import os
import sys
from datetime import datetime, timezone
from unittest import mock

from fairshare import cli, store
from fairshare.model import SessionSeen, Worker
from fairshare.providers import claude

from test_honest import NOW
from test_workers import PARENT, Work


class Watch(Work):
    def setUp(self):
        super().setUp()
        self.probes, self.listed = [], []
        p = mock.patch.object(cli, "_start_probe", lambda *a: self.probes.append(a[0].id))
        p.start()
        self.addCleanup(p.stop)
        real = claude.list_sessions

        def counted(acct):
            self.listed.append(self.clock[0] - NOW)
            return real(acct)
        p = mock.patch.object(claude, "list_sessions", counted)
        p.start()
        self.addCleanup(p.stop)

    def watch(self, *argv, session=PARENT):
        env = {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a"),
               **({"CLAUDE_CODE_SESSION_ID": session} if session else {})}
        with mock.patch.dict(os.environ, env):
            return self.go("watch", *argv)

    def worker(self, name="w-1", live=True, started=NOW - 120):
        """A joined worker of PARENT on b, started (by default) longer ago than the grace a new
        one gets before it is judged ended."""
        sid = f"s-{name}"
        w = Worker(name, PARENT, "b", str(self.here), started)
        store.add_worker(w)
        cli._joined(w, sid, NOW)
        if live:
            with open(self.log / "live", "a") as f:
                f.write(f"{name}\n")
        return sid

    def test_outside_a_session_exits_2(self):
        self.assertEqual(self.watch(session=None)[0], 2)

    def test_timeout_and_cadence(self):
        self.worker()
        code, out, _ = self.watch("--timeout", "31")
        self.assertEqual((code, out), (0, "nothing happened\n"))
        self.assertEqual(self.slept, [5.0] * 6 + [1.0])          # local state every 5 s
        self.assertEqual(self.listed, [0.0, 15.0, 30.0])         # the listing every 15 s

    def test_no_workers_no_listing(self):
        self.watch("--timeout", "20")
        self.assertEqual(self.listed, [])

    def stage(self, five=92):
        """Account a at `five`% of its 5h window, which resets in 3 hours: from 90%, its handoff
        stage."""
        self.snap("a", five=(five, 3), seven=(10, 50))

    def test_its_accounts_handoff_stage_once_across_restarts(self):
        self.stage()
        code, out, _ = self.watch("--timeout", "10")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("fairshare: account a is in its handoff stage: write your "
                                       "checkpoint and hand off now"), out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(self.watch("--timeout", "10")[1], "nothing happened\n")
        self.stage(80)                                           # credited back: it ends
        self.assertEqual(self.watch("--timeout", "10")[1],
                         "fairshare: the handoff stage on account a is over: keep working here.\n")

    def test_a_workers_account_in_its_handoff_stage(self):
        sid = self.worker()
        self.snap("b", five=(95, 3), seven=(40, 50))
        self.assertEqual(self.watch("--timeout", "10")[1], (
            "fairshare: account b, where worker w-1 runs, is in its handoff stage: run fairshare "
            f"pickup {sid[:8]} to continue the worker on a.\n"))

    def test_a_line_a_hook_said_is_not_said_again(self):
        self.stage()
        raw = json.dumps({"session_id": PARENT, "cwd": str(self.here)}).encode()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            self.assertIn("handoff stage", self.go("hook", "post-tool")[1])
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_the_handoff_stage_while_waiting_wakes_it(self):
        def later(s):
            self.sleep(s)
            if self.clock[0] - NOW >= 10:
                self.stage(95)
        with mock.patch.object(cli, "_sleep", later):
            code, out, _ = self.watch("--timeout", "60")
        self.assertIn("is in its handoff stage", out)
        self.assertEqual(self.clock[0] - NOW, 10)

    def test_a_worker_checkpoint_once_then_again_when_it_grows(self):
        sid = self.worker()
        path = store.append_checkpoint(sid, "## t\nhalf\n")
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 wrote a checkpoint: {path}\n")
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
        store.append_checkpoint(sid, "## t\nall\n")
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 wrote a checkpoint: {path}\n")

    def test_a_stranded_worker(self):
        sid = self.worker()
        store.note_session(SessionSeen(sid), "b", NOW, stranded=True)
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 was stopped by b's limit: fairshare pickup {sid}\n")
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_a_stranded_worker_says_when_its_limit_resets(self):
        """Stage 5c: the provider resumes it then by itself; the main session decides."""
        sid = self.worker()
        self.snap("b", five=(100, 2), seven=(40, 50))
        store.note_session(SessionSeen(sid), "b", NOW, stranded=True)
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 was stopped by b's limit: resets 10:00 (Claude Code resumes "
                         f"it then); fairshare pickup {sid}\n")

    def test_a_worker_that_ended(self):
        self.worker(live=False)
        self.assertEqual(self.watch("--timeout", "5")[1], "worker w-1 ended\n")
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_idle_after_busy(self):
        self.worker()
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")   # seen busy
        (self.log / "live").write_text("")
        idle = [claude.LiveSession("s-w-1", "w-1", False)]
        with mock.patch.object(claude, "list_sessions", lambda acct: idle):
            self.assertEqual(self.watch("--timeout", "5")[1], "worker w-1 went idle\n")
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_a_worker_waiting_for_an_answer_is_said_once_per_wait(self):
        """A question or a permission prompt holds a worker in the middle of a turn: said with
        the command that opens it, even after a turn end its hook stamped, once per wait."""
        sid = self.worker()
        self.stopped(sid, NOW - 60)                        # an earlier turn ended
        self.assertIn("ended its turn", self.watch("--timeout", "5")[1])
        listed = [claude.LiveSession(sid, "w-1", None, waiting=True)]
        line = (f"worker w-1 is waiting for an answer (a question or a permission prompt) that "
                f"only a person can give: fairshare run -a b attach {sid[:8]}\n")
        with mock.patch.object(claude, "list_sessions", lambda acct: listed):
            self.assertEqual(self.watch("--timeout", "5")[1], line)
            self.assertEqual(self.watch("--timeout", "20")[1], "nothing happened\n")
            listed[0] = claude.LiveSession(sid, "w-1", True)          # answered, working again
            self.assertEqual(self.watch("--timeout", "5")[1],         # no transcript: plain
                             "worker w-1 no longer waits (answered, or timed out)\n")
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
            listed[0] = claude.LiveSession(sid, "w-1", None, waiting=True)   # a new prompt
            self.assertEqual(self.watch("--timeout", "5")[1], line)

    def decided(self, sid, decision, source, before=0.0, **extra):
        """A permission decision appended to worker `sid`'s transcript, stamped `before` seconds
        before the time its current wait was first said."""
        path = self.tmp / f"{sid}.jsonl"
        store.update_session(sid, lambda r: cli.replace(r, transcript=str(path)))
        at = store.session(PARENT).reported[f"waiting:{sid}"] - before
        stamp = datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")
        with open(path, "a") as f:
            f.write(json.dumps({"type": "user", "timestamp": stamp, "permissionDecision": {
                "decision": decision, "source": source, **extra}}) + "\n")

    def test_how_a_wait_ended_is_said_once(self):
        """When a wait it said clears, watch says once how it ended, from the worker's
        transcript; a new wait is said again, then its own end."""
        sid = self.worker()
        listed = [claude.LiveSession(sid, "w-1", None, waiting=True)]
        with mock.patch.object(claude, "list_sessions", lambda acct: listed):
            self.assertIn("is waiting for an answer", self.watch("--timeout", "5")[1])
            self.decided(sid, "accept", "user_temporary", before=3600)   # an older prompt
            self.decided(sid, "reject", "config", reasonType="safetyCheck")
            listed[0] = claude.LiveSession(sid, "w-1", True)
            self.assertEqual(self.watch("--timeout", "5")[1], "worker w-1 no longer waits: "
                             "nobody answered, so it was denied (the prompt timed out)\n")
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
            listed[0] = claude.LiveSession(sid, "w-1", None, waiting=True)
            self.assertIn("is waiting for an answer", self.watch("--timeout", "5")[1])
            self.decided(sid, "accept", "user_temporary")
            listed[0] = claude.LiveSession(sid, "w-1", True)
            self.assertEqual(self.watch("--timeout", "5")[1],
                             "worker w-1 no longer waits: a person allowed it\n")

    def test_a_worker_that_ends_while_waiting(self):
        sid = self.worker()
        listed = [claude.LiveSession(sid, "w-1", None, waiting=True)]
        with mock.patch.object(claude, "list_sessions", lambda acct: listed):
            self.assertIn("is waiting for an answer", self.watch("--timeout", "5")[1])
            self.decided(sid, "reject", "user_reject")
            listed.clear()
            self.assertEqual(self.watch("--timeout", "5")[1], "worker w-1 no longer waits: a "
                             "person denied it\nworker w-1 ended\n")

    def test_no_wait_end_when_no_wait_was_said(self):
        sid = self.worker()
        listed = [claude.LiveSession(sid, "w-1", True)]
        with mock.patch.object(claude, "list_sessions", lambda acct: listed):
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
            listed[0] = claude.LiveSession(sid, "w-1", False)
            self.assertEqual(self.watch("--timeout", "5")[1], "worker w-1 went idle\n")
            listed.clear()
            self.assertEqual(self.watch("--timeout", "5")[1], "worker w-1 ended\n")

    def test_the_listing_reads_waiting(self):
        raw = [{"sessionId": "s-1", "name": "w", "status": "waiting", "kind": "background",
                "pid": 7}, {"sessionId": "s-2", "status": "busy"}]
        with mock.patch.object(claude.subprocess, "run", lambda *a, **k: mock.Mock(
                stdout=json.dumps(raw))):
            got = claude.list_sessions(cli._load()[0][0])
        self.assertEqual([(s.waiting, s.busy) for s in got], [(True, None), (False, True)])

    def test_each_worker_in_its_own_accounts_listing(self):
        """A background session is listed only through the account it runs on: a worker on
        another account than the first one's is neither ended at once nor missed at its end."""
        self.worker("w-1")
        store.add_worker(Worker("w-2", PARENT, "a", str(self.here), NOW - 119))
        cli._joined(store.worker("w-2"), "s-w-2", NOW - 119)
        by = {"b": [claude.LiveSession("s-w-1", "w-1", True)],
              "a": [claude.LiveSession("s-w-2", "w-2", True)]}
        with mock.patch.object(claude, "list_sessions", lambda acct: by[acct.id]):
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
            by["a"] = []
            self.assertEqual(self.watch("--timeout", "5")[1], "worker w-2 ended\n")
            by["b"] = None                      # b's listing failed: w-1 is not judged
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def stopped(self, sid, at, tasks=0):
        store.update_session(sid, lambda r: cli.replace(r, stopped=[at, tasks]))

    def test_a_turn_end_once_though_the_listing_says_busy(self):
        """The listing can say busy for hours after a worker's turn ended: its own Stop hook's
        stamp is said once, and the busy mark goes, so no later idle says it again."""
        sid = self.worker()
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")   # seen busy
        self.assertEqual(store.session(PARENT).reported, {f"busy:{sid}": 1})
        self.stopped(sid, NOW)
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 ended its turn and is waiting: fairshare transcript "
                         f"{sid[:8]} --tail 1\n")
        self.assertNotIn(f"busy:{sid}", store.session(PARENT).reported)
        self.assertEqual(self.watch("--timeout", "20")[1], "nothing happened\n")  # still busy
        self.assertNotIn(f"busy:{sid}", store.session(PARENT).reported)
        idle = [claude.LiveSession(sid, "w-1", False)]
        with mock.patch.object(claude, "list_sessions", lambda acct: idle):
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
        self.stopped(sid, NOW + 60)                    # its next turn end is news
        self.assertIn("ended its turn", self.watch("--timeout", "5")[1])

    def test_a_turn_end_with_background_tasks_says_so(self):
        sid = self.worker()
        self.stopped(sid, NOW, tasks=1)
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 ended its turn with 1 background task still running (it "
                         f"wakes when that ends): fairshare transcript {sid[:8]} --tail 1\n")
        self.stopped(sid, NOW + 1, tasks=3)
        self.assertIn("with 3 background tasks still running (it wakes when one ends)",
                      self.watch("--timeout", "5")[1])

    def test_a_turn_end_before_its_limit_stop_is_said_as_the_limit_stop_only(self):
        sid = self.worker()
        self.stopped(sid, NOW - 30)
        store.note_session(SessionSeen(sid), "b", NOW, stranded=True)
        self.assertEqual(self.watch("--timeout", "5")[1],
                         f"worker w-1 was stopped by b's limit: fairshare pickup {sid}\n")
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
        self.stopped(sid, NOW + 10)            # resumed later (at the reset) and ended a turn
        self.assertIn("worker w-1 ended its turn", self.watch("--timeout", "5")[1])

    def test_a_new_worker_not_listed_yet_is_not_ended(self):
        self.worker(live=False, started=NOW - 10)
        self.assertEqual(self.watch("--timeout", "45")[1], "nothing happened\n")
        self.assertEqual(self.watch("--timeout", "30")[1], "worker w-1 ended\n")  # grace over

    def test_a_checkpoint_copied_by_a_pickup_is_not_new(self):
        sid = self.worker()
        store.append_checkpoint(sid, "## t\nhalf\n")
        self.watch("--timeout", "5")
        store.link_session(sid, SessionSeen("s-new", str(self.here)), "a", NOW)
        store.join_worker("w-1", "s-new")
        with open(self.log / "live", "a") as f:
            f.write("s-new\n")
        with mock.patch.object(claude, "list_sessions",
                               lambda acct: [claude.LiveSession("s-new", "w-1", True)]):
            self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
            path = store.append_checkpoint("s-new", "## t\nall\n")
            self.assertEqual(self.watch("--timeout", "5")[1],
                             f"worker w-1 wrote a checkpoint: {path}\n")

    def test_several_events_one_line_each(self):
        sid = self.worker()
        self.worker("w-2", live=False)
        path = store.append_checkpoint(sid, "x\n")
        out = self.watch("--timeout", "5")[1]
        self.assertEqual(out, f"worker w-1 wrote a checkpoint: {path}\nworker w-2 ended\n")

    def test_a_pending_join_happens_here(self):
        store.add_worker(Worker("w-late", PARENT, "b", str(self.here), NOW))
        (self.log / "live").write_text("w-late\n")
        self.watch("--timeout", "1")
        self.assertEqual(store.worker("w-late").session, "s-w-late")

    def test_another_sessions_workers_are_not_its_concern(self):
        w = Worker("theirs", "someone-else", "b", str(self.here), NOW)
        store.add_worker(w)
        cli._joined(w, "s-theirs", NOW)
        store.append_checkpoint("s-theirs", "x\n")
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    # A worker its main session removed (`worker --remove`) is not spoken of: no checkpoint,
    # limit stop, turn end, waiting, idle or ended line; one not removed still is.

    def things(self, name):
        """A worker with a checkpoint, a limit stop and a later turn end, no longer listed."""
        sid = self.worker(name, live=False)
        store.append_checkpoint(sid, "## t\nhalf\n")
        store.note_session(SessionSeen(sid), "b", NOW, stranded=True)
        self.stopped(sid, NOW + 1)
        return sid

    def test_a_removed_worker_says_nothing_not_removed_still_does(self):
        self.things("w-kept")
        self.things("w-gone")
        store.remove_worker("w-gone", NOW)
        code, out, _ = self.watch("--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("worker w-kept wrote a checkpoint", out)
        self.assertIn("worker w-kept was stopped by b's limit", out)
        self.assertIn("worker w-kept ended", out)
        self.assertNotIn("w-gone", out)
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_the_lines_a_removed_worker_would_have_had(self):
        sid = self.things("w-1")
        self.assertEqual(len(self.watch("--timeout", "5")[1].splitlines()), 4)   # control
        store.remove_worker("w-1", NOW)
        store.append_checkpoint(sid, "## t\nall\n")                   # a new checkpoint
        self.stopped(sid, NOW + 60)                                    # a new turn end
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_a_removed_worker_is_not_waiting_or_idle_or_looked_for(self):
        sid = self.worker()
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")   # seen busy
        store.remove_worker("w-1", NOW)
        asked = []

        def listing(acct):
            asked.append(acct.id)
            return [claude.LiveSession(sid, "w-1", None, waiting=True)]
        with mock.patch.object(claude, "list_sessions", listing):
            self.assertEqual(self.watch("--timeout", "20")[1], "nothing happened\n")
            listed = [claude.LiveSession(sid, "w-1", False)]            # idle after busy
            with mock.patch.object(claude, "list_sessions", lambda acct: listed):
                self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")
        self.assertEqual(asked, [])                                     # no listing for it at all

    def test_a_removed_worker_that_is_listed_no_more_is_not_said_ended(self):
        self.worker(live=False)
        store.remove_worker("w-1", NOW)
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_status_still_lists_the_session_of_a_removed_worker(self):
        """`status` reads the listing, not the worker records."""
        self.worker()
        before = self.go("status")[1]
        self.assertIn("session s-w-1", before)
        store.remove_worker("w-1", NOW)
        self.assertEqual(self.go("status")[1], before)
