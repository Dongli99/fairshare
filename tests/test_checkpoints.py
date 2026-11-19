"""Stage 5b, part B: checkpoints. The fleet of test_workers (temp HOME, config, state and hub;
fakes first on PATH; inherited session variables cleared)."""
import os
from unittest import mock

from fairshare import cli, store
from fairshare.model import ROLE_WORKER, SessionSeen, Worker

from test_honest import NOW
from test_workers import PARENT, Work

W1, W2 = "s-w-1", "s-w-2"


class Checkpoints(Work):
    def setUp(self):
        super().setUp()
        for name, sid, parent in (("w-1", W1, PARENT), ("w-2", W2, PARENT),
                                  ("other", "s-other", "someone-else")):
            w = Worker(name, parent, "b", str(self.here), NOW)
            store.add_worker(w)
            cli._joined(w, sid, NOW)

    def cp(self, *argv, session=PARENT):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session} if session else {}):
            return self.go("checkpoint", *argv)

    def test_append_with_the_local_time(self):
        code, out, _ = self.cp("parser done; next: tests", session=W1)
        path = self.tmp / "state" / "checkpoints" / f"{W1}.md"
        self.assertEqual((code, out), (0, f"{path}\n"))
        self.clock[0] += 300
        self.cp("tests done; next: docs", session=W1)
        self.assertEqual(path.read_text(), "## 2027-01-15 08:00\nparser done; next: tests\n"
                                           "## 2027-01-15 08:05\ntests done; next: docs\n")

    def test_outside_a_session_or_without_text(self):
        self.assertEqual(self.cp("x", session=None)[0], 2)
        self.assertEqual(self.cp()[0], 2)
        self.assertEqual(store.checkpointed(), [])

    def test_a_request_is_set_and_the_checkpoint_clears_it(self):
        code, out, _ = self.cp("--request", "w-1")
        self.assertEqual((code, out), (0, "w-1 (session s-w-1): asked for a checkpoint\n"))
        self.assertEqual(store.session(W1).checkpoint_requested, NOW)
        self.assertIsNone(store.session(W2).checkpoint_requested)
        self.cp("done", session=W1)
        rec = store.session(W1)
        self.assertEqual((rec.checkpoint_requested, rec.role, rec.parent),
                         (None, ROLE_WORKER, PARENT))

    def test_a_request_by_session_prefix(self):
        self.assertEqual(self.cp("--request", "s-w-2")[1],
                         "w-2 (session s-w-2): asked for a checkpoint\n")
        code, _, err = self.cp("--request", "s-w")
        self.assertEqual(code, 2)
        self.assertIn("matches 2 workers", err)
        self.assertEqual(self.cp("--request", "nobody")[0], 2)

    def test_all_means_every_worker_of_the_calling_session(self):
        code, out, _ = self.cp("--request", "all")
        self.assertEqual(out, "w-1 (session s-w-1): asked for a checkpoint\n"
                              "w-2 (session s-w-2): asked for a checkpoint\n")
        self.assertIsNone(store.session("s-other").checkpoint_requested)
        self.assertEqual(self.cp("--request", "all", session=None)[0], 2)
        self.assertEqual(self.cp("--request", "all", session="lonely")[1],
                         "this session has no workers\n")

    def test_all_skips_a_worker_watch_saw_end_but_a_named_one_is_asked(self):
        store.note_session(SessionSeen(PARENT), "a", NOW)
        store.update_session(PARENT, lambda r: cli.replace(r, reported={f"ended:{W2}": 1}))
        self.assertEqual(self.cp("--request", "all"),
                         (0, "skipped 1 worker that ended\n"
                             "w-1 (session s-w-1): asked for a checkpoint\n", ""))
        self.assertIsNone(store.session(W2).checkpoint_requested)
        self.assertEqual(self.cp("--request", "w-2")[1],
                         "w-2 (session s-w-2): asked for a checkpoint\n")
        store.update_session(PARENT, lambda r: cli.replace(
            r, reported={f"ended:{W1}": 1, f"ended:{W2}": 1}))
        self.assertEqual(self.cp("--request", "all"), (0, "skipped 2 workers that ended\n", ""))

    def test_a_worker_not_joined_yet_is_not_asked(self):
        store.add_worker(Worker("new", PARENT, "b", str(self.here), NOW))
        with mock.patch.dict(os.environ, {"FAKE_NO_LIST": "1"}):
            code, out, _ = self.cp("--request", "new")
        self.assertEqual((code, out), (1, "new: its session id is not known yet; not asked "
                                          "(try again shortly)\n"))
        (self.log / "live").write_text("new\n")                 # it shows up: joined and asked
        self.assertEqual(self.cp("--request", "new")[1],
                         "new (session s-new): asked for a checkpoint\n")
        self.assertEqual(store.session("s-new").checkpoint_requested, NOW)

    def test_show(self):
        self.cp("half way", session=W1)
        for target in ("w-1", "s-w-1", "s-w-"):
            with self.subTest(target=target):
                self.assertEqual(self.cp("--show", target),
                                 (0, "## 2027-01-15 08:00\nhalf way\n", ""))
        code, _, err = self.cp("--show", "w-2")
        self.assertEqual(code, 1)
        self.assertIn("no checkpoint note for 'w-2'", err)

    def test_the_pickup_prompt_reads_the_note_first(self):
        store.note_session(SessionSeen("gone", str(self.here), "/t/x.jsonl"), "a", NOW,
                           stranded=True)
        rec = store.session("gone")
        self.assertNotIn("checkpoint", cli._pickup_prompt(rec, NOW))
        path = store.append_checkpoint("gone", "## t\nwhere it stood\n")
        prompt = cli._pickup_prompt(rec, NOW)
        first = (f"Read its checkpoint note {path} first; it is the session's own account of "
                 "where it stood.")
        self.assertIn(f"Its folder: {self.here}. {first} Run `fairshare transcript gone", prompt)
