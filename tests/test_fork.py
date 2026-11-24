"""`pickup` is the same session, never a copy; `fork` is a copy on purpose; the session list both
show from a terminal. The fleet of test_handover (accounts a, out; b, ranked first; c; the fake
claude lists `$FAKE_LOG/live` with each session's kind and process, and logs a `stop`)."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import ROLE_WORKER, LiveSession, SessionRecord, SessionSeen, Worker
from fairshare.providers import claude

from test_handover import Handover

MOVED_SAME = ("fairshare restarted this session on account a because {why}; it is still the best "
              "account. Continue where you left off.")
from test_hub import write
from test_honest import NOW
from test_pickup import SID, TRANSCRIPT

ID8 = SID[:8]
FORK_HINT = f"`fairshare fork {ID8}` continues a copy and leaves it running"


class Running(Handover):
    def live(self, *lines):
        (self.log / "live").write_text("".join(f"{x}\n" for x in lines))

    def stops(self):
        path = self.log / "stops"
        return path.read_text().split() if path.exists() else []


class PickupNeverCopies(Running):
    """`fairshare pickup` of a session its account may still run."""

    def setUp(self):
        super().setUp()
        self.record(stranded=None, model="claude-opus-5-5", effort="high")

    def started(self):
        cmd = self.launched.call_args.args[0]
        self.assertNotIn("--fork-session", cmd)
        return cmd

    def test_not_running_resumes_in_place(self):
        code, _, err = self.go("pickup", ID8, tty=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started()[5:7], ["--resume", SID])
        self.assertEqual(self.stops(), [])

    def test_a_background_session_with_no_process_is_not_running(self):
        self.live(f"{SID} x-1 background null null")
        code, _, err = self.go("pickup", ID8, tty=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started()[5:7], ["--resume", SID])
        self.assertEqual(self.stops(), [])

    def test_background_yes_stops_it_and_resumes_in_place_keeping_its_name(self):
        self.live(f"{SID} x-1 background 100 busy")
        code, _, err = self.go("pickup", ID8, tty=True, answers=[""])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stops(), [SID])
        self.assertEqual(self.started()[5:9], ["--name", "x-1", "--resume", SID])
        self.assertIn(f"fairshare: {ID8} is in the middle of a turn: stopping it loses that turn "
                      "(its conversation is kept)\n"
                      f"fairshare: {ID8} is still running in the background. Stop it and "
                      f"continue it here? [Y/n] fairshare: stopped {ID8}\n", err)
        env = self.launched.call_args.args[1]
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}")

    def test_background_yes_with_bg_resumes_it_in_the_background(self):
        self.live(f"{SID} x-1 background 100 idle")
        code, _, err = self.go("pickup", ID8, "--bg", tty=True, answers=["y"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("in the middle of a turn", err)            # idle: no turn lost
        self.assertEqual(self.stops(), [SID])
        self.assertEqual(self.bg_args("b")[:5], ["--bg", "--name", "x-1", "--resume", SID])
        self.assertNotIn("--fork-session", self.bg_args("b"))
        self.assertEqual([(r.id, r.account) for r in store.sessions()], [(SID, "b")])

    def test_background_no_does_nothing_and_names_fork(self):
        self.live(f"{SID} x-1 background 100 busy")
        code, _, err = self.go("pickup", ID8, tty=True, answers=["n"])
        self.assertEqual(code, 1)
        self.assertTrue(err.endswith(f"fairshare: nothing done; {FORK_HINT}\n"), err)
        self.assertEqual(self.stops(), [])
        self.launched.assert_not_called()

    def test_end_of_input_is_no(self):
        self.live(f"{SID} x-1")
        code, _, err = self.go("pickup", ID8, tty=True, answers=[EOFError()])
        self.assertEqual((code, self.stops()), (1, []))
        self.launched.assert_not_called()

    def test_a_failed_stop_does_nothing(self):
        self.live(f"{SID} x-1")
        with mock.patch.dict(os.environ, {"FAKE_STOP_EXIT": "1"}):
            code, _, err = self.go("pickup", ID8, tty=True, answers=[""])
        self.assertEqual(code, 1)
        self.assertTrue(err.endswith(f"fairshare: Claude Code did not stop {ID8}; nothing "
                                     "done\n"), err)
        self.launched.assert_not_called()

    def test_still_listed_after_the_stop_does_nothing(self):
        self.live(f"{SID} x-1")
        with mock.patch.dict(os.environ, {"FAKE_STOP_STAYS": "1"}):
            code, _, err = self.go("pickup", ID8, tty=True, answers=[""])
        self.assertEqual(code, 1)
        self.assertEqual(self.stops(), [SID])
        self.assertTrue(err.endswith(f"fairshare: {ID8} is still listed running 5 s after it was "
                                     "stopped; nothing done\n"), err)
        self.assertLessEqual(sum(self.slept), cli.STOP_WAIT + cli.JOIN_POLL)
        self.launched.assert_not_called()

    def test_an_interactive_session_is_never_stopped(self):
        self.live(f"{SID} x-1 interactive 4242 busy")
        for argv in (("pickup", ID8), ("pickup", ID8, "--bg")):
            for tty in (True, False):
                with self.subTest(argv=argv, tty=tty):
                    code, _, err = self.go(*argv, tty=tty, answers=[""])
                    self.assertEqual(code, 1)
                    self.assertTrue(err.endswith(
                        f"fairshare: {ID8} is open in a terminal; not picked up. Exit it there, "
                        f"then pick it up (it resumes in place); or {FORK_HINT}\n"), err)
                    self.assertEqual(self.asked.call_count, 0)
        self.assertEqual((self.stops(), self.bg_args("b")), ([], []))
        self.launched.assert_not_called()

    def test_with_no_terminal_nothing_is_stopped(self):
        self.live(f"{SID} x-1")
        code, _, err = self.go("pickup", ID8)
        self.assertEqual(code, 1)
        self.assertTrue(err.endswith(
            f"fairshare: {ID8} is still running; not picked up. If it is yours, stop it first "
            f"(fairshare run -a a stop {SID}), then pick it up; or {FORK_HINT}\n"), err)
        self.assertEqual((self.stops(), self.bg_args("b")), ([], []))
        self.assertEqual(store.session(SID).account, "a")

    def test_from_inside_another_session_nothing_is_stopped(self):
        self.live(f"{SID} x-1")
        code, _, err = self.inside("pickup", ID8, sid="s-other")
        self.assertEqual((code, self.stops(), self.bg_args("b")), (1, [], []))
        self.assertIn("is still running; not picked up", err)

    def test_without_a_list_nothing_is_stopped(self):
        with mock.patch.object(claude, "list_sessions", lambda acct: None):
            code, _, err = self.go("pickup", ID8, "--bg", tty=True, answers=[""])
        self.assertEqual(code, 1)
        self.assertIn(f"{ID8} may still be running (its account's list of running sessions "
                      "cannot be had); not picked up", err)
        self.assertEqual((self.stops(), self.bg_args("b")), ([], []))

    def test_a_stranded_session_still_listed_is_asked_about_too(self):
        store.update_session(SID, lambda old: SessionRecord(**{**vars(old), "stranded_at": NOW}))
        self.live(f"{SID} x-1")                    # left waiting for its reset: still running
        code, _, err = self.go("pickup", ID8, tty=True, answers=["n"])
        self.assertEqual((code, self.stops()), (1, []))
        self.assertIn("still running in the background", err)

    def test_no_account_can_work_stops_nothing(self):
        self.snap("b", five=(100, 2), seven=(80, 150))
        self.snap("c", five=(100, 0.5), seven=(90, 150))
        self.live(f"{SID} x-1")
        for argv in (("pickup", ID8), ("pickup", ID8, "--bg")):
            code, _, err = self.go(*argv, tty=True, answers=[""])
            self.assertEqual(code, 1)
            self.assertIn("no account can take work now", err)
        self.assertEqual(self.stops(), [])


class OwnAccountBackground(Running):
    """A background pickup on the session's own account of a session known to run in the
    background passes nothing but the resume and the prompt: the tool keeps such a session's
    own saved options and copies on any other argument (claude.RESUME_BARE)."""

    def setUp(self):
        super().setUp()
        self.snap("a", five=(10, 3), seven=(10, 20))                 # a ranks first
        self.record(stranded=None, model="claude-opus-5-5", effort="high")
        (self.log / "ended").write_text(f"{SID} saved background null null\n")   # `--all`

    def test_not_known_as_background_keeps_the_flags_and_allows_explicit_ones(self):
        (self.log / "ended").unlink()
        code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("a")[:9], ["--bg", "--name", "pickup-0a1b2c3d", "--resume",
                                                 SID, "--model", "claude-opus-5-5", "--effort",
                                                 "high"])
        (self.log / "live").unlink()                                 # that resume stopped
        code, out, err = self.go("pickup", SID, "--bg", "--model", "sonnet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("a")[5:7], ["--model", "sonnet"])

    def test_an_interactive_one_among_the_ended_or_no_list_is_not_known(self):
        (self.log / "ended").write_text(f"{SID} saved interactive null null\n")
        self.go("pickup", SID, "--bg")
        self.assertEqual(self.bg_args("a")[1:3], ["--name", "pickup-0a1b2c3d"])
        (self.log / "live").unlink()                                 # that resume stopped
        (self.log / "ended").write_text(f"{SID} saved background null null\n")
        real = claude.list_sessions
        with mock.patch.object(claude, "list_sessions",
                               lambda acct, ended=False: None if ended else real(acct)):
            code, _, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("a")[1:3], ["--name", "pickup-0a1b2c3d"])

    def test_in_place_with_no_flags_and_no_copy(self):
        with mock.patch.dict(os.environ, {"FAKE_BG_STATE": SID}):
            code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("a"), ["--bg", "--resume", SID,
                                             MOVED_SAME.format(why="a move was asked for")])
        self.assertNotIn("copy", err + out)
        self.assertEqual([(r.id, r.account, r.moved_to) for r in store.sessions()],
                         [(SID, "a", None)])

    def test_a_stopped_worker_keeps_its_name_without_passing_it(self):
        (self.log / "ended").unlink()                                # a worker is known anyway
        store.add_worker(Worker("w-1", "p-1", "a", str(self.work), NOW, SID, "haiku", "none"))
        self.live(f"{SID} w-1 background 100 idle")
        with mock.patch.dict(os.environ, {"FAKE_BG_STATE": SID}):
            code, out, err = self.go("pickup", SID, "--bg", tty=True, answers=[""])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stops(), [SID])
        self.assertEqual(self.bg_args("a")[:3], ["--bg", "--resume", SID])
        self.assertEqual(len(self.bg_args("a")), 4)

    def test_explicit_model_or_effort_is_refused_naming_fork(self):
        for flags, said in ((["--model", "sonnet"], "--model sonnet"),
                            (["--effort", "max"], "--effort max"),
                            (["--model", "sonnet", "--effort", "low"],
                             "--model sonnet --effort low")):
            with self.subTest(flags=flags):
                self.live(f"{SID} x-1")
                code, _, err = self.go("pickup", SID, "--bg", *flags, tty=True, answers=[""])
                self.assertEqual(code, 1)
                self.assertTrue(err.endswith(
                    f"fairshare: {ID8} resumes in the background on its own account a only as it "
                    f"was, with its own model and effort, so {said} cannot apply in place; "
                    f"nothing done. For a copy with them: fairshare fork {ID8} {said}\n"), err)
                self.assertEqual((self.stops(), self.bg_args("a")), ([], []))

    def test_another_account_is_a_move_with_the_flags(self):
        code, out, err = self.go("pickup", SID, "--bg", "-a", "b", "--model", "sonnet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[:7], ["--bg", "--name", "pickup-0a1b2c3d", "--resume",
                                                 SID, "--model", "sonnet"])
        self.assertNotIn("copy", err)

    def test_a_copy_the_tool_made_is_said_with_its_note_and_linked(self):
        note = (f"note: session {ID8} is already running in the background, so this started a "
                "copy as cafe0001. `claude attach 0a1b2c3d` opens the original.")
        with mock.patch.dict(os.environ, {"FAKE_COPY": "1", "FAKE_COPY_NOTE": note}):
            code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{note}\nfairshare: Claude Code did not continue {ID8} in place but "
                      f"started a copy under a new id ({note})\n", err)
        self.assertNotIn("in place\n", out)
        self.assertEqual(store.session(SID).moved_to, None)          # unknown id: not moved
        self.assertIn("the copy's session id is not known yet", out)

    def test_a_copy_found_by_name_is_linked(self):
        store.add_worker(Worker("w-1", "p-1", "a", str(self.work), NOW, SID, "haiku", "none"))
        wrapper = self.bin / "claude"
        wrapper.write_text(wrapper.read_text().replace('n=saved;', 'n=w-1;'))
        with mock.patch.dict(os.environ, {"FAKE_COPY": "1"}):
            code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Claude Code did not continue {ID8} in place but started a copy under a "
                      "new id\n", err)
        self.assertIn("continued as session s-w-1 on a", out)
        self.assertEqual(store.session("s-w-1").continues, SID)

    def test_copy_note(self):
        self.assertIsNone(claude.copy_note("To attach: claude attach cafe0001\n"))
        for line in ("note: background session 0a1b2c3d keeps its own saved options, so the "
                     "flags you passed started a copy as cafe0001. Without flags, the same "
                     "command continues 0a1b2c3d itself.",
                     "note: a restricted shell does not wake background session 0a1b2c3d in "
                     "place, so this started a restricted copy as cafe0001."):
            self.assertEqual(claude.copy_note(f"warning: x\n{line}\n"), line)


class OwnWorker(Running):
    """A main session picks up its own worker: stopped without asking, then resumed in place."""

    def setUp(self):
        super().setUp()
        self.record(sid="p-1", stranded=None)
        store.add_worker(Worker("w-1", "p-1", "a", str(self.work), NOW, SID, "haiku", "none"))
        self.record(stranded=None)
        store.update_session(SID, lambda old: SessionRecord(
            **{**vars(old), "role": ROLE_WORKER, "parent": "p-1"}))

    def test_its_main_session_stops_it_without_asking_and_resumes_it_in_place(self):
        self.live(f"{SID} w-1 background 100 idle")
        code, out, err = self.inside("pickup", SID, sid="p-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stops(), [SID])
        self.assertEqual(self.asked.call_count, 0)
        self.assertNotIn("turn", err)
        self.assertIn(f"fairshare: stopped {ID8}\n", err)
        self.assertEqual(self.bg_args("b")[:5], ["--bg", "--name", "w-1", "--resume", SID])
        self.assertNotIn("--fork-session", self.bg_args("b"))

    def test_busy_says_the_turn_is_lost(self):
        self.live(f"{SID} w-1 background 100 busy")
        code, out, err = self.inside("pickup", SID, sid="p-1")
        self.assertEqual(code, 0, err)
        self.assertIn(f"fairshare: stopping worker w-1 ({ID8}) in the middle of a turn: that turn "
                      "is lost (its conversation is kept)\n", err)

    def test_stopped_by_its_limit_says_nothing_extra(self):
        store.update_session(SID, lambda old: SessionRecord(**{**vars(old), "stranded_at": NOW,
                                                               "waiting_until": NOW + 3600}))
        self.live(f"{SID} w-1 background 100 busy")
        code, out, err = self.inside("pickup", SID, sid="p-1")
        self.assertEqual((code, self.stops()), (0, [SID]))
        self.assertNotIn("turn", err)

    def test_its_main_session_under_a_new_id_still_counts(self):
        self.record(sid="p-2", stranded=None)              # p-1 went on as p-2; the worker's
        store.update_session("p-1", lambda old: SessionRecord(   # record still names p-1
            **{**vars(old), "moved_to": "p-2"}))
        self.live(f"{SID} w-1")
        code, out, err = self.inside("pickup", SID, sid="p-2")
        self.assertEqual((code, self.stops()), (0, [SID]), err)

    def test_anyone_else_gets_the_refusal(self):
        self.live(f"{SID} w-1")
        for sid in ("s-other", None):
            with self.subTest(sid=sid):
                code, _, err = self.inside("pickup", SID, sid=sid) if sid else \
                    self.go("pickup", SID, "--bg")
                self.assertEqual(code, 1)
                self.assertIn(f"{ID8} is still running; not picked up", err)
        self.assertEqual(self.stops(), [])

    def test_a_failed_stop_or_one_still_listed_refuses(self):
        self.live(f"{SID} w-1")
        for env, said in (({"FAKE_STOP_EXIT": "1"}, "did not stop"),
                          ({"FAKE_STOP_STAYS": "1"}, "still listed running")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                code, _, err = self.inside("pickup", SID, sid="p-1")
                self.assertEqual(code, 1)
                self.assertIn(said, err)
        self.assertEqual(self.bg_args("b"), [])


class Fork(Running):
    """`fairshare fork`: a copy under a new id; the original is never stopped."""

    def setUp(self):
        super().setUp()
        self.record(stranded=None, model="claude-opus-5-5", effort="high")

    def test_foreground_from_a_terminal_in_the_sessions_folder(self):
        self.live(f"{SID} x-1 background 100 busy")
        code, _, err = self.go("fork", ID8, tty=True)
        self.assertEqual(code, 0, err)
        cmd, env, = self.launched.call_args.args[:2]
        self.assertEqual(cmd[1:], ["--model", "claude-opus-5-5", "--effort", "high", "--name",
                                   "x-1-f2", "--resume", SID, "--fork-session",
                                   cli.FORKED_PROMPT.format(old=SID, new="b")])
        self.assertEqual(Path(os.getcwd()).resolve(), self.work.resolve())
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}:copy")
        self.assertEqual(self.asked.call_count, 0)
        self.assertEqual(self.stops(), [])
        rec = store.session(SID)
        self.assertEqual((rec.account, rec.moved_to), ("a", None))

    def test_a_stopped_session_forks_too(self):
        code, _, err = self.go("fork", ID8, tty=True)
        self.assertEqual(code, 0, err)
        cmd = self.launched.call_args.args[0]
        self.assertEqual(cmd[5:10], ["--name", "fork-0a1b2c3d", "--resume", SID,
                                     "--fork-session"])

    def test_inside_a_session_bare_fork_is_this_one_in_the_background_never_the_launcher(self):
        launcher = self.started()
        code, out, err = self.inside("fork", launcher=launcher, tty=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[:6], ["--bg", "--name", "fork-0a1b2c3d", "--resume",
                                                 SID, "--fork-session"])
        self.assertIsNone(store.take_request(launcher.pid))          # the launcher not asked
        self.assertIsNone(launcher.poll())
        self.launched.assert_not_called()
        self.assertIn("continued as session s-fork-0a1b2c3d on b", out)
        self.assertEqual(store.session("s-fork-0a1b2c3d").continues, SID)

    def test_a_workers_fork_leaves_the_worker_where_it_is(self):
        store.add_worker(Worker("w-1", "p-1", "a", str(self.work), NOW, "s-w", "haiku", "none"))
        self.record(sid="s-w", stranded=None)
        store.update_session("s-w", lambda old: SessionRecord(
            **{**vars(old), "role": ROLE_WORKER, "parent": "p-1"}))
        self.live("s-w w-1")
        code, out, err = self.go("fork", "s-w", "--bg")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[:6], ["--bg", "--name", "w-1-f2", "--resume", "s-w",
                                                 "--fork-session"])
        self.assertEqual([w.session for w in store.workers()], ["s-w"])
        copy = store.session("s-w-1-f2")
        self.assertEqual((copy.continues, copy.role, copy.parent), ("s-w", ROLE_WORKER, "p-1"))
        self.assertEqual(store.session("s-w").moved_to, None)

    def test_no_terminal_forks_in_the_background(self):
        code, _, err = self.go("fork", ID8)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[3:6], ["--resume", SID, "--fork-session"])

    def test_names(self):
        rec = store.session(SID)
        for live, name in (([], "fork-0a1b2c3d"),
                           (None, "fork-0a1b2c3d"),
                           ([LiveSession("x", "fork-0a1b2c3d", None)], "fork-0a1b2c3d-f2"),
                           ([LiveSession(SID, "w", None)], "w-f2"),
                           ([LiveSession(SID, "w-f2", None)], "w-f3"),
                           ([LiveSession(SID, "w", None), LiveSession("x", "w-f2", None),
                             LiveSession("y", "w-f3", None)], "w-f4")):
            with self.subTest(live=live):
                self.assertEqual(cli._fork_name(rec, live), name)

    def test_the_forks_first_prompt_says_it_is_a_copy(self):
        self.assertEqual(cli.FORKED_PROMPT.format(old="s-1", new="b"),
                         "fairshare forked this session, as asked: it is a copy of session s-1 "
                         "under a new id, on account b, and the original goes on by itself. Do "
                         "not take up its work unasked: say in one line that you are the copy, "
                         "then wait for what to do.")


class SessionList(Running):
    """Bare `pickup` / `fork` from a terminal outside a session."""

    def setUp(self):
        super().setUp()
        self.other = self.tmp / "other"
        self.other.mkdir()
        for x in "abc":
            write(self.tmp / "hub/accounts" / x / ".claude.json", json.dumps({"projects": {
                str(p.resolve()): {"hasTrustDialogAccepted": True}
                for p in (self.work, self.other)}}))
        os.chdir(self.work)

    def add_rec(self, sid, cwd=None, seen=NOW - 60, stranded=None, transcript=TRANSCRIPT,
                account="a", **extra):
        store.note_session(SessionSeen(sid, str(cwd or self.work),
                                       str(transcript) if transcript else None),
                           account, seen, stranded=stranded)
        if extra:
            store.update_session(sid, lambda old: SessionRecord(**{**vars(old), **extra}))

    def rows(self, err):
        return [line for line in err.splitlines() if line.startswith("  ")]

    def test_order_filters_state_and_last_prompt(self):
        self.add_rec("s-here-old", seen=NOW - 900)
        self.add_rec("s-here-new", seen=NOW - 100)
        self.add_rec("s-here-out", seen=NOW - 2000, stranded=True, limits={"a": NOW + 3600})
        self.add_rec("s-there", cwd=self.other, seen=NOW - 10)
        self.add_rec("s-wait", cwd=self.other, seen=NOW - 3000, stranded=True,
                     waiting_until=NOW + 1800)
        self.add_rec("s-worker", seen=NOW, role=ROLE_WORKER)
        self.add_rec("s-moved", seen=NOW, moved_to="s-here-new")
        self.add_rec("s-gone", cwd=self.tmp / "gone", seen=NOW)
        self.live("s-here-new n1 background 100 busy", "s-there n2 interactive 7 idle")
        code, out, err = self.go("pickup", tty=True, answers=[EOFError()])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.rows(err), [
            '  1  s-here-o  work   a  at its limit, resets 09:00                  seen 07:26  '
            '"Now add a test for December to January."',
            '  2  s-here-n  work   a  running in the background, asks to stop it  seen 07:58  '
            '"Now add a test for December to January."',
            '  3  s-here-o  work   a  stopped                                     seen 07:45  '
            '"Now add a test for December to January."',
            '  4  s-wait    other  a  waiting for its reset at 08:30              seen 07:10  '
            '"Now add a test for December to January."',
            '  5  s-there   other  a  open in a terminal                          seen 07:59  '
            '"Now add a test for December to January."'])
        self.assertIn("fairshare: workers are not listed (their main session manages them); "
                      "fairshare pickup <id> and fairshare fork <id> still take one by id\n", err)
        self.assertTrue(err.endswith("\nfairshare: nothing done\n"), err)
        self.assertEqual(self.asked.call_args.args, ("Pick up which? [1]: ",))
        code, out, err = self.go("fork", tty=True, answers=[KeyboardInterrupt()])
        self.assertEqual(code, 1)
        self.assertIn("  2  s-here-n  work   a  running in the background       seen 07:58", err)
        self.assertIn("  5  s-there   other  a  running in a terminal           seen 07:59", err)
        self.assertEqual(self.asked.call_args.args, ("Fork which? [1]: ",))
        self.launched.assert_not_called()

    def test_enter_picks_the_first_and_takes_the_session_path(self):
        self.add_rec("s-1", seen=NOW - 10)
        self.add_rec("s-2", seen=NOW - 20)
        code, _, err = self.go("pickup", tty=True, answers=[""])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.launched.call_args.args[0][1:3], ["--resume", "s-1"])

    def test_a_number_picks_and_junk_asks_again(self):
        self.add_rec("s-1", seen=NOW - 10)
        self.add_rec("s-2", cwd=self.other, seen=NOW - 20)
        code, _, err = self.go("fork", tty=True, answers=["x", "9", "0", "2"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.asked.call_count, 4)
        cmd = self.launched.call_args.args[0]
        self.assertEqual(cmd[3:6], ["--resume", "s-2", "--fork-session"])
        self.assertEqual(Path(os.getcwd()).resolve(), self.other.resolve())

    def test_no_terminal_prints_the_list_to_stderr_and_exits_2(self):
        self.add_rec("s-1")
        for verb in ("pickup", "fork"):
            code, out, err = self.go(verb)
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(len(self.rows(err)), 1)
            self.assertTrue(err.endswith(f"fairshare: no terminal to ask which; name one: "
                                         f"fairshare {verb} SESSION\n"), err)
            self.assertEqual(self.asked.call_count, 0)

    def test_none_to_offer(self):
        self.add_rec("s-w", role=ROLE_WORKER)
        code, _, err = self.go("pickup", tty=True)
        self.assertEqual((code, err), (2, "fairshare: no recorded session to pickup\n"))

    def test_at_most_eight_rows(self):
        for n in range(11):
            self.add_rec(f"s-{n:02}", seen=NOW - n, transcript=None)
        code, _, err = self.go("pickup", tty=True, answers=[EOFError()])
        rows = self.rows(err)
        self.assertEqual(len(rows), cli.LIST_ROWS)
        self.assertTrue(rows[0].startswith("  1  s-00"), rows[0])
        self.assertTrue(rows[0].endswith("seen 08:00"), rows[0])     # no transcript: blank

    def test_a_list_that_cannot_be_had_is_said(self):
        self.add_rec("s-1")
        with mock.patch.object(claude, "list_sessions", lambda acct: None):
            code, _, err = self.go("pickup", tty=True, answers=[EOFError()])
        self.assertIn("may be running (no list)", self.rows(err)[0])

    def test_one_listing_per_account(self):
        self.add_rec("s-1")
        self.add_rec("s-2", account="b")
        self.add_rec("s-3")
        calls = []
        real = claude.list_sessions
        with mock.patch.object(claude, "list_sessions",
                               lambda acct: calls.append(acct.id) or real(acct)):
            self.go("fork", tty=True, answers=[EOFError()])
        self.assertEqual(sorted(calls), ["a", "b"])


class LastPrompt(Running):
    """The adapter's last prompt: what a person typed, read from the transcript's end only."""

    def write(self, *records, pad=0):
        path = self.tmp / "t.jsonl"
        filler = json.dumps({"type": "assistant", "isSidechain": False, "message": {
            "content": [{"type": "text", "text": "x" * 1000}]}}) + "\n"
        with path.open("w") as f:
            for r in records:
                f.write((json.dumps(r) if isinstance(r, dict) else r) + "\n")
            f.write(filler * pad)
        return path

    @staticmethod
    def user(text, **extra):
        return {"type": "user", "isSidechain": False, "message": {"content": text}, **extra}

    def test_the_fixture(self):
        self.assertEqual(claude.last_prompt(TRANSCRIPT), "Now add a test for December to January.")

    def test_injected_texts_commands_and_the_tools_own_records_are_skipped(self):
        path = self.write(self.user("the real one", promptSource="typed"),
                          self.user("<bash-input>ls</bash-input>"),
                          self.user("<bash-stdout>a</bash-stdout><bash-stderr></bash-stderr>"),
                          self.user("<task-notification>done</task-notification>",
                                    promptSource="system"),
                          self.user("Caveat: injected", isMeta=True),
                          self.user("This session is being continued", isCompactSummary=True),
                          self.user("background notice", promptSource="system"),
                          self.user([{"type": "tool_result", "content": "out"}]),
                          self.user("side", isSidechain=True),
                          "not json")
        self.assertEqual(claude.last_prompt(path), "the real one")

    def test_only_the_end_is_read_widened_once(self):
        read = []
        real = claude._tail
        with mock.patch.object(claude, "_tail", lambda p, size: read.append(size) or real(p, size)):
            near = self.write(self.user("near"), pad=300)               # ~300 KB after it
            self.assertEqual(claude.last_prompt(near), "near")
            self.assertEqual(read, [claude.TAIL_BYTES, claude.TAIL_BYTES * claude.TAIL_WIDER])
            read.clear()
            far = self.write(self.user("far"), pad=2500)                # ~2.5 MB after it
            self.assertIsNone(claude.last_prompt(far))                  # never the whole file
            self.assertEqual(read, [claude.TAIL_BYTES, claude.TAIL_BYTES * claude.TAIL_WIDER])
            self.assertGreater(far.stat().st_size, claude.TAIL_BYTES * claude.TAIL_WIDER)

    def test_fairshares_own_prompts_are_skipped_by_their_leading_text(self):
        own = cli._own_prompts()
        sent = [cli.MOVED_PROMPT.format(old="a", new="b", why="x"),
                cli.RESTARTED_PROMPT.format(new="a", why="x"),
                cli.FORKED_PROMPT.format(old="s-1", new="b"),
                "This session continues session s-1, which stopped ...",
                cli.WORKER_LINE.format(parent="p-1") + "\n\nDo the task.",
                cli.HANDOFF_NOTE_ASK]
        path = self.write(self.user("fairshare status says b is best; pick it", promptSource="typed"),
                          *(self.user(t, promptSource="queued") for t in sent))
        self.assertEqual(claude.last_prompt(path, own),
                         "fairshare status says b is best; pick it")      # a person's "fairshare"
        self.assertEqual(claude.last_prompt(path), cli.HANDOFF_NOTE_ASK)

    def test_the_list_shows_the_prompt_before_fairshares(self):
        store.note_session(SessionSeen("s-1", str(self.work), str(self.write(
            self.user("the real one"), self.user(cli.MOVED_PROMPT.format(old="a", new="b",
                                                                          why="x"))))), "a", NOW)
        os.chdir(self.work)
        code, _, err = self.go("pickup", tty=True, answers=[EOFError()])
        self.assertIn('"the real one"', err)

    def test_unreadable_or_empty(self):
        self.assertIsNone(claude.last_prompt(self.tmp / "missing.jsonl"))
        self.assertIsNone(claude.last_prompt(self.write()))


class WaitOutcome(Running):
    """How a permission prompt ended, from the permission decisions at a transcript's end."""
    write, user = LastPrompt.write, LastPrompt.__dict__["user"]
    WAIT = 1_800_000_000.0                                   # when the wait was first said

    def decision(self, decision, source, at=WAIT + 5, **extra):
        stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"type": "user", "timestamp": stamp, "permissionDecision": {
                    "decision": decision, "source": source, **extra}}

    def test_each_decision(self):
        cases = [(("accept", "user_temporary"), "allowed"), (("reject", "user_reject"), "denied"),
                 (("reject", "config"), "timed out"), (("accept", "config"), None),
                 (("ask", "user"), None)]
        for args, want in cases:
            path = self.write(self.decision(*args, reasonType="safetyCheck"))
            self.assertEqual(claude.wait_outcome(path, self.WAIT), want, args)
        for source, reason in (("config", "classifier"), ("hook", "hook")):   # no prompt
            path = self.write(self.decision("reject", source, reasonType=reason))
            self.assertIsNone(claude.wait_outcome(path, self.WAIT), source)

    def test_the_first_decision_after_the_wait_ended_it(self):
        path = self.write(self.decision("reject", "config", reasonType="safetyCheck"),
                          self.decision("accept", "config", at=self.WAIT + 9,
                                        reasonType="classifier"))       # its next tool call
        self.assertEqual(claude.wait_outcome(path, self.WAIT), "timed out")

    def test_a_decision_before_the_wait_is_not_its_end(self):
        path = self.write(self.decision("accept", "user_temporary", at=self.WAIT - 5))
        self.assertIsNone(claude.wait_outcome(path, self.WAIT))
        path = self.write(self.decision("reject", "config", at=self.WAIT - 600,
                                        reasonType="safetyCheck"))
        self.assertIsNone(claude.wait_outcome(path, self.WAIT))
        self.assertEqual(claude.wait_outcome(path, None), "timed out")   # an old mark: any

    def test_a_torn_last_line_and_other_records(self):
        path = self.write(self.decision("reject", "user_reject"), self.user("go on"))
        with open(path, "a") as f:
            f.write('{"type": "user", "permissionDecision": {"decision": "acc')
        self.assertEqual(claude.wait_outcome(path, self.WAIT), "denied")

    def test_no_transcript_or_no_decision(self):
        self.assertIsNone(claude.wait_outcome(self.tmp / "missing.jsonl", self.WAIT))
        self.assertIsNone(claude.wait_outcome(self.write(self.user("a question?")), self.WAIT))


if __name__ == "__main__":
    import unittest
    unittest.main()
