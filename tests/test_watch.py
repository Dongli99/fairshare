"""Stage 5b, part D: `fairshare watch`. The fleet of test_workers (temp HOME, config, state and
hub; fakes first on PATH; inherited session variables cleared); the clock and the sleep are
injected (Fleet), so the cadence is read off the clock. The watching session runs on a."""
import io
import json
import os
import sys
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

    def worker(self, name="w-1", live=True):
        sid = f"s-{name}"
        w = Worker(name, PARENT, "b", str(self.here), NOW)
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

    def test_its_account_crossing_once_across_restarts(self):
        self.snap("a", five=(92, 3), seven=(10, 50))
        code, out, _ = self.watch("--timeout", "10")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("fairshare: account a is at 92% of its 5h window"), out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(self.watch("--timeout", "10")[1], "nothing happened\n")

    def test_a_crossing_a_hook_said_is_not_said_again(self):
        self.snap("a", five=(92, 3), seven=(10, 50))
        raw = json.dumps({"session_id": PARENT, "cwd": str(self.here)}).encode()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            self.assertIn("92%", self.go("hook", "post-tool")[1])
        self.assertEqual(self.watch("--timeout", "5")[1], "nothing happened\n")

    def test_a_crossing_while_waiting_wakes_it(self):
        def later(s):
            self.sleep(s)
            if self.clock[0] - NOW >= 10:
                self.snap("a", five=(95, 3), seven=(10, 50))
        with mock.patch.object(cli, "_sleep", later):
            code, out, _ = self.watch()
        self.assertIn("at 95% of its 5h window", out)
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
