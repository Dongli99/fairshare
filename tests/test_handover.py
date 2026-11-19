"""Stage 5c, part D: model and effort, a pickup that resumes the same session, handoff, switch and
the worker's model. Same fleet as test_pickup (temp HOME, config, state and hub; fake claude and
fairshare first on PATH, proved in Fleet.setUp; inherited CLAUDE*, FAIRSHARE_* and BROWSER
cleared). The fake claude is wrapped so that a background start lists its session by name, and
a headless `-p --resume` turn answers with $FAKE_NOTE. Signals go only to processes the tests
start."""
import io
import json
import os
import pty
import signal
import subprocess
import sys
import termios
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from fairshare import cli, session, store
from fairshare.model import (EFFORT_NONE, ROLE_WORKER, Request, SessionRecord, SessionSeen,
                             Snapshot, Window, Worker)
from fairshare.providers import claude

from test_honest import H, NOW, Fleet
from test_hub import write
import test_pickup
from test_pickup import SID, TRANSCRIPT, Stranded
from test_session import WAIT

WRAPPER = """#!/bin/sh
# A background start records its arguments and folder and goes live under its name: as the
# session it resumes (in place), or as s-<name> (a fork, or a fresh session). `agents --json`
# lists the live ones. A headless turn (`-p` with `--resume`) logs its arguments and answers
# $FAKE_NOTE (after $FAKE_NOTE_SLEEP seconds, exiting $FAKE_NOTE_EXIT).
name=$(basename "$CLAUDE_CONFIG_DIR")
if [ "$1" = "agents" ]; then
  printf '['; sep=
  [ -f "$FAKE_LOG/live" ] && while read id n; do
    printf '%s{"sessionId": "%s", "name": "%s", "status": "busy"}' "$sep" "$id" "$n"; sep=,
  done < "$FAKE_LOG/live"
  printf ']\\n'; exit 0
fi
if [ "$1" = "--bg" ]; then
  pwd -P > "$FAKE_LOG/cwd-$name"
  printf '%s\\n' "$@" > "$FAKE_LOG/bg-$name"
  live="s-$3"; fork=; prev=
  for x; do
    [ "$prev" = "--resume" ] && resumed=$x
    [ "$x" = "--fork-session" ] && fork=1
    prev=$x
  done
  [ -n "$resumed" ] && [ -z "$fork" ] && [ -z "$FAKE_COPY" ] && live=$resumed
  echo "$live $3" >> "$FAKE_LOG/live"
fi
note=
for x; do [ "$x" = "--resume" ] && note=1; done
if [ "$1" = "-p" ] && [ -n "$note" ]; then
  printf '%s\\n' "$@" > "$FAKE_LOG/note-$name"
  pwd -P > "$FAKE_LOG/note-cwd-$name"
  [ -n "$FAKE_NOTE_SLEEP" ] && sleep "$FAKE_NOTE_SLEEP"
  printf '%s' "$FAKE_NOTE"; exit "${FAKE_NOTE_EXIT:-0}"
fi
exec "{real}" "$@"
"""

NOTE_JSON = json.dumps({"type": "result", "is_error": False, "result": "Done: parser. Next: tests."})
MOVED = ("fairshare moved this session from account a to account {new} because {why}. Continue "
         "where you left off.")
LIMIT = "a hit its usage limit"
ASKED = "a move was asked for"
NEAR = "a is near its usage limit"


class Handover(Stranded):
    """Accounts a (out for three hours), b (ranks first) and c; a session record on a."""

    def setUp(self):
        super().setUp()
        self.add("c", orgId="org-c")
        self.snap("c", five=(10, 2), seven=(60, 90))
        real = self.bin / "claude-real"
        os.rename(self.bin / "claude", real)
        write(self.bin / "claude", WRAPPER.replace("{real}", str(real))).chmod(0o755)
        for x in "abc":
            self.trust(x)

    def record(self, sid=SID, account="a", stranded=True, cwd=None, model=None, effort=None,
               name=None):
        store.note_session(SessionSeen(sid, str(cwd or self.work), str(TRANSCRIPT), model,
                                       name or (model and model.title()), effort),
                           account, NOW - 60, stranded=stranded)

    def opus_spent(self, *ids):
        for i in ids:
            snap = store.load(i)
            store.save(i, Snapshot(snap.captured_at, {**snap.windows,
                                                     "seven_day:Opus": Window(100, NOW + 30 * H)}))

    def bg_args(self, account):
        path = self.log / f"bg-{account}"
        return path.read_text().splitlines() if path.exists() else []

    def launches(self, *argv, requests=(), tty=False, answers=()):
        """The CLI with run_child faked: the n-th launch, when `requests[n]` is a Request, files
        it for this process and asks, as SIGUSR1 would, then returns what the real one would."""
        self.calls = []

        def run_child(cmd, env, on_request=None):
            self.calls.append((cmd, env, os.getcwd()))
            n = len(self.calls) - 1
            if n < len(requests) and requests[n]:
                store.save_request(os.getpid(), requests[n])
                return session.PICKUP if on_request(4321) else 0
            return 0
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(session, "run_child", run_child), \
                mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch.object(cli, "_countdown", return_value=True), \
                mock.patch("builtins.input", side_effect=list(answers)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(list(argv))
        self.out = out.getvalue()
        return code, err.getvalue()

    def started_args(self, n=1):
        """The arguments (after the binary) of the n-th launch, and its account."""
        cmd, env, _ = self.calls[n]
        return cmd[1:], Path(env["CLAUDE_CONFIG_DIR"]).name

    def inside(self, *argv, launcher=None, tty=False, sid=SID):
        """`fairshare ...` run inside session `sid`, under the stand-in `launcher` when given."""
        env = {"CLAUDE_CODE_SESSION_ID": sid, "CLAUDE_PID": "4321",
               "CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}
        if launcher:
            env["FAIRSHARE_LAUNCHER"] = str(launcher.pid)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, "_ancestors",
                                  return_value={launcher.pid} if launcher else set()):
            return self.go(*argv, tty=tty)


class LatestModel(Handover):
    """D1: every capture overwrites the model and effort; no model keeps them."""

    def capture(self, **payload):
        raw = json.dumps({"session_id": SID, "cwd": str(self.work), **payload})
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw.encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/b")}):
            return self.go("capture")

    def model(self):
        r = store.session(SID)
        return r.model, r.model_name, r.effort

    def test_the_latest_overwrites_and_no_model_keeps_what_is_known(self):
        self.capture(model={"id": "claude-opus-5-5", "display_name": "Opus"},
                     effort={"level": "high"})
        self.assertEqual(self.model(), ("claude-opus-5-5", "Opus", "high"))
        self.capture(model={"id": "claude-opus-5-5", "display_name": "Opus"},
                     effort={"level": "max"})                        # /effort mid-session
        self.assertEqual(self.model(), ("claude-opus-5-5", "Opus", "max"))
        self.capture()                                               # no model: keep
        self.capture(model={"display_name": "x"}, effort={"level": "low"})   # no id: keep
        self.assertEqual(self.model(), ("claude-opus-5-5", "Opus", "max"))

    def test_a_model_without_effort_records_none(self):
        self.capture(model={"id": "claude-haiku-5-5", "display_name": "Haiku"})
        self.assertEqual(self.model(), ("claude-haiku-5-5", "Haiku", EFFORT_NONE))
        self.assertEqual(json.loads((self.tmp / "state/sessions" / f"{SID}.json").read_text())
                         ["effort"], "none")

    def test_hook_input_never_sets_the_model(self):
        self.capture(model={"id": "claude-opus-5-5", "display_name": "Opus"},
                     effort={"level": "high"})
        raw = {"session_id": SID, "cwd": "/w", "model": {"id": "other"},
               "hook_event_name": "UserPromptSubmit"}
        self.assertIsNone(claude.hook_session("prompt", raw).model)
        self.hook({"model": {"id": "other"}})                       # a stop-failure input
        self.assertEqual(self.model(), ("claude-opus-5-5", "Opus", "high"))

    def test_model_args(self):
        self.assertEqual(claude.model_args("opus", "high"), ["--model", "opus", "--effort", "high"])
        self.assertEqual(claude.model_args("haiku", EFFORT_NONE), ["--model", "haiku"])
        self.assertEqual(claude.model_args(None, "low"), ["--effort", "low"])
        self.assertEqual(claude.model_args(None, None), [])

    def test_a_model_week_is_matched_by_its_words(self):
        for model, week, uses in (("claude-opus-5-5", "Opus", True), ("opus", "Opus", True),
                                  ("Opus", "Opus", True), ("claude-sonnet-5-5", "Opus", False),
                                  ("claude-fable-5-2", "Fable", True), ("x", "", False)):
            with self.subTest(model=model, week=week):
                self.assertEqual(claude.uses_week(model, week), uses)


class LauncherPickup(Handover):
    """D0 and D2 under the launcher: the same session, resumed with its model and effort."""

    def test_automatic_and_manual_resume_the_same_id_with_model_and_effort(self):
        self.policy("resume_wait_minutes = 30")                     # opted in; a is out for 3h
        for manual, why in ((False, LIMIT), (True, NEAR)):          # manual: a's 5h at 100%
            with self.subTest(manual=manual):
                self.record(model="claude-opus-5-5", effort="high", stranded=not manual)
                code, err = self.launches("run", "-a", "a", requests=[Request(SID, manual, 4321)])
                self.assertEqual(code, 0, err)
                args, acct = self.started_args()
                self.assertEqual((args, acct), (["--model", "claude-opus-5-5", "--effort", "high",
                                                 "--resume", SID,
                                                 MOVED.format(new="b", why=why)], "b"))
                rec = store.session(SID)          # its account: written by its first capture
                self.assertEqual((rec.account, rec.stranded_at, rec.model, rec.effort),
                                 ("a", None, "claude-opus-5-5", "high"))
                self.assertEqual([r.id for r in store.sessions()], [SID])   # not duplicated
                store.move_session(SID, "a")

    def test_a_record_with_neither_starts_as_today(self):
        self.policy("resume_wait_minutes = 30")
        self.record()
        self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])

    def test_the_flags_override_the_record_and_a_skips_the_ranking(self):
        self.record(model="claude-opus-5-5", effort="high")
        code, err = self.launches("run", "-a", "a", requests=[
            Request(SID, True, 4321, "pickup", "sonnet", "low", "c")])
        self.assertEqual(code, 0, err)
        args, acct = self.started_args()
        self.assertEqual((args[:4], acct), (["--model", "sonnet", "--effort", "low"], "c"))
        self.assertEqual(args[4:6], ["--resume", SID])

    def test_an_account_with_that_models_week_spent_is_skipped(self):
        self.policy("resume_wait_minutes = 30")
        self.record(model="claude-opus-5-5", effort="high")
        self.opus_spent("b")
        self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual(self.started_args()[1], "c")

    def test_when_no_account_has_it_an_automatic_move_waits_instead(self):
        self.policy("resume_wait_minutes = 30")
        self.record(model="claude-opus-5-5", effort="high")
        self.opus_spent("b", "c")
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, False, 4321)])
        self.assertEqual((code, len(self.calls)), (0, 1))            # the child runs on
        self.assertEqual(store.session(SID).waiting_until, NOW + 3 * H)

    def test_when_no_account_has_it_a_terminal_asks(self):
        self.record(model="claude-opus-5-5", effort="high")
        self.opus_spent("b", "c")
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)],
                                  tty=True, answers=["d"])
        self.assertEqual(code, 0, err)
        self.assertIn("no account that can take work has claude-opus-5-5 left this week", err)
        args, acct = self.started_args()
        self.assertEqual((args[:2], acct), (["--resume", SID], "b"))    # its default model
        self.record(model="claude-opus-5-5", effort="high")             # stranded again on a
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)],
                                  tty=True, answers=["q"])
        self.assertEqual((code, len(self.calls)), (0, 1))
        self.assertIn("not started", err)

    def test_when_no_account_has_it_without_a_terminal_it_refuses_naming_model(self):
        self.record(model="claude-opus-5-5", effort="high")
        self.opus_spent("b", "c")
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321)])
        self.assertEqual((code, len(self.calls)), (1, 1))
        self.assertIn("left this week; name another with --model", err)


class CommandPickup(Handover):
    """D0 and D2 from `fairshare pickup` outside the launcher."""

    def test_foreground_resumes_with_the_flags(self):
        self.record(model="claude-opus-5-5", effort="high")
        code, _, err = self.go("pickup", SID[:8], "--effort", "max", tty=True)
        self.assertEqual(code, 0, err)
        cmd = self.launched.call_args.args[0]
        self.assertEqual(cmd[1:6], ["--model", "claude-opus-5-5", "--effort", "max", "--resume"])
        self.assertEqual(store.session(SID).account, "a")     # until its first capture

    def test_foreground_of_a_running_session_continues_a_copy(self):
        self.record(stranded=None, model="claude-opus-5-5", effort="high")
        code, _, err = self.go("pickup", SID[:8], tty=True)
        self.assertEqual(code, 0, err)
        cmd, env = self.launched.call_args.args[:2]
        self.assertEqual(cmd[5:8], ["--resume", SID, "--fork-session"])
        self.assertIn(f"{SID[:8]} was not stopped by its limit and may still be running; "
                      "continuing a copy under a new id (the original is left as it is)", err)
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}:copy")
        rec = store.session(SID)                                 # left as it is
        self.assertEqual((rec.account, rec.stranded_at, rec.moved_to), ("a", None, None))

    def test_foreground_of_a_stranded_session_resumes_in_place(self):
        self.record(model="claude-opus-5-5", effort="high")
        code, _, err = self.go("pickup", SID[:8], tty=True)
        self.assertEqual(code, 0, err)
        cmd, env = self.launched.call_args.args[:2]
        self.assertEqual(cmd[5:7], ["--resume", SID])
        self.assertNotIn("--fork-session", cmd)
        self.assertNotIn("may still be running", err)
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}")
        self.assertIsNone(store.session(SID).stranded_at)       # cleared at the start

    def test_background_of_a_stranded_session_resumes_in_place(self):
        self.record(model="claude-opus-5-5", effort="high")
        code, out, err = self.inside("pickup")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b"), ["--bg", "--name", "pickup-0a1b2c3d", "--resume", SID,
                                             "--model", "claude-opus-5-5", "--effort", "high",
                                             MOVED.format(new="b", why=LIMIT)])
        self.assertEqual(Path((self.log / "cwd-b").read_text().strip()), self.work.resolve())
        self.assertEqual([(r.id, r.account, r.stranded_at) for r in store.sessions()],
                         [(SID, "b", None)])

    def test_background_of_a_running_session_forks_with_a_copied_record(self):
        self.record(stranded=None, model="claude-opus-5-5", effort="high")
        store.update_session(SID, lambda old: SessionRecord(
            **{**vars(old), "role": ROLE_WORKER, "parent": "p-1"}))
        code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        args = self.bg_args("b")
        self.assertEqual(args[3:6], ["--resume", SID, "--fork-session"])
        new = "s-pickup-0a1b2c3d"
        self.assertIn(f"continued as session {new} on b", out)
        old, copy = store.session(SID), store.session(new)
        self.assertEqual((old.account, old.role), ("a", ROLE_WORKER))           # left as it was
        self.assertEqual((copy.account, copy.role, copy.parent, copy.model, copy.effort,
                          copy.cwd), ("b", ROLE_WORKER, "p-1", "claude-opus-5-5", "high",
                                      str(self.work)))

    def test_a_second_pickup_of_the_same_session_links_the_new_copy_never_the_earlier(self):
        self.record(stranded=None, model="claude-opus-5-5", effort="high")
        (self.log / "live").write_text("s-earlier pickup-0a1b2c3d\n")   # an earlier pickup, live
        code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        new = "s-pickup-0a1b2c3d"
        self.assertIn(f"continued as session {new} on b", out)
        self.assertEqual(store.session(new).continues, SID)
        self.assertIsNone(store.session("s-earlier"))
        self.assertFalse(store.checkpoint_path("s-earlier").is_file())

    def test_a_copy_the_tool_made_by_itself_gets_the_record_too(self):
        self.record(model="claude-opus-5-5", effort="high")
        with mock.patch.dict(os.environ, {"FAKE_COPY": "1"}):
            code, out, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 0, err)
        self.assertNotIn("--fork-session", self.bg_args("b"))
        self.assertEqual(store.session("s-pickup-0a1b2c3d").account, "b")
        self.assertEqual(store.session(SID).account, "a")

    def test_background_refuses_when_no_account_has_the_model(self):
        self.record(model="claude-opus-5-5", effort="high")
        self.opus_spent("b", "c")
        code, _, err = self.go("pickup", SID, "--bg")
        self.assertEqual(code, 1)
        self.assertIn("no account that can take work has claude-opus-5-5 left this week; name "
                      "another with --model", err)
        self.assertEqual(self.bg_args("b"), [])
        code, _, err = self.go("pickup", SID, "--bg", "--model", "sonnet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[5:9], ["--model", "sonnet", "--effort", "high"])

    def test_an_unknown_account_is_refused_before_anything(self):
        self.record()
        code, _, err = self.go("pickup", SID, "--bg", "-a", "zz")
        self.assertEqual(code, 2)
        self.assertIn("no account 'zz'", err)

    def test_a_worker_keeps_its_name_and_the_worker_values_until_a_capture(self):
        store.add_worker(Worker("w-1", "p-1", "a", str(self.work), NOW, "s-w", "haiku",
                                EFFORT_NONE))
        self.record(sid="s-w")
        self.go("pickup", "s-w", "--bg")
        self.assertEqual(self.bg_args("b")[:6], ["--bg", "--name", "w-1", "--resume", "s-w",
                                                 "--model"])
        self.assertEqual(self.bg_args("b")[6:], ["haiku", MOVED.format(new="b", why=LIMIT)])
        store.move_session("s-w", "a")
        store.update_session("s-w", lambda old: SessionRecord(
            **{**vars(old), "stranded_at": NOW - 30, "model": "claude-opus-5-5",
               "effort": "high"}))
        self.go("pickup", "s-w", "--bg")
        self.assertEqual(self.bg_args("b")[5:9], ["--model", "claude-opus-5-5", "--effort",
                                                  "high"])


class Handoff(Handover):
    """D4."""

    def note(self):
        return store.checkpoint_path(SID).read_text()

    def test_outside_a_session_it_refuses(self):
        code, _, err = self.go("handoff", "x")
        self.assertEqual(code, 2)
        self.assertIn("fairshare pickup SESSION", err)

    def test_with_a_note_and_a_launcher_it_checkpoints_then_asks(self):
        self.record(stranded=None)
        launcher = self.started()
        code, out, err = self.inside("handoff", "Done A; next B.", "--model", "opus",
                                     launcher=launcher)
        self.assertEqual(code, 0, err)
        self.assertEqual(launcher.wait(timeout=WAIT), -signal.SIGUSR1)
        self.assertEqual(store.take_request(launcher.pid),
                         Request(SID, True, 4321, "handoff", "opus", name="work-h2"))
        self.assertTrue(self.note().endswith("\nDone A; next B.\n"))

    def test_with_a_note_and_no_launcher_a_fresh_background_session(self):
        self.record(model="claude-opus-5-5", effort="high")
        code, out, err = self.inside("handoff", "Done A.")
        self.assertEqual(code, 0, err)
        args = self.bg_args("b")
        self.assertEqual(args[:7], ["--bg", "--name", "work-h2", "--model",
                                    "claude-opus-5-5", "--effort", "high"])
        self.assertNotIn("--resume", args)
        self.assertIn(f"Read its checkpoint note {store.checkpoint_path(SID)} first", args[7])
        self.assertIn(f"continues session {SID}, which handed over at 08:00 and may still be "
                      "running. Its folder:", args[7])
        self.assertNotIn("stopped", args[7])
        self.assertIn(f"handing off {SID[:8]} to a fresh session on b, in the background\n", err)
        self.assertNotIn("picking up", err)
        self.assertTrue(out.endswith("this session keeps running: stop here and leave the work "
                                     "to the new session\n"), out)

    def test_without_a_note_it_needs_a_launcher(self):
        self.record()
        code, _, err = self.inside("handoff")
        self.assertEqual(code, 1)
        self.assertIn("/fairshare handoff", err)
        self.assertFalse(store.checkpoint_path(SID).exists())

    def test_without_a_note_it_asks_the_launcher(self):
        self.record()
        launcher = self.started()
        code, out, _ = self.inside("handoff", "-a", "c", launcher=launcher)
        self.assertEqual(code, 0)
        self.assertEqual(launcher.wait(timeout=WAIT), -signal.SIGUSR1)
        self.assertEqual(store.take_request(launcher.pid),
                         Request(SID, True, 4321, "handoff-note", account="c", name="work-h2"))

    def test_the_fresh_session_is_named_after_the_listed_name_with_the_next_number(self):
        self.record(stranded=None)
        for listed, named in (("fairshare-6a", "fairshare-6a-h2"), ("x-h2", "x-h3"),
                              ("x-h9", "x-h10"), ("ab-h", "ab-h-h2")):
            with self.subTest(listed=listed):
                (self.log / "live").write_text(f"s-other other\n{SID} {listed}\n")
                launcher = self.started()
                self.assertEqual(self.inside("handoff", launcher=launcher)[0], 0)
                launcher.wait(timeout=WAIT)
                self.assertEqual(store.take_request(launcher.pid).name, named)

    def test_without_a_listed_name_the_folder_or_session_is_named(self):
        self.record(stranded=None)
        rec = store.session(SID)
        self.assertEqual(cli._handoff_name(rec), "work-h2")           # not listed
        with mock.patch.object(claude, "list_sessions", lambda acct: None):
            self.assertEqual(cli._handoff_name(rec), "work-h2")       # no listing
        with mock.patch.object(claude, "list_sessions",
                               lambda acct: [claude.LiveSession(SID, None, True)]):
            self.assertEqual(cli._handoff_name(rec), "work-h2")       # listed with no name
            self.assertEqual(cli._handoff_name(replace(rec, cwd=None)), "session-h2")

    def test_a_workers_handoff_keeps_the_workers_name(self):
        store.add_worker(Worker("fs-parser", "p-1", "a", str(self.work), NOW, SID))
        self.record(stranded=None)
        (self.log / "live").write_text(f"{SID} fs-parser\n")
        code, _, err = self.inside("handoff", "Done A.")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[:3], ["--bg", "--name", "fs-parser"])

    def test_the_launcher_starts_a_fresh_session_for_a_written_note(self):
        self.record(model="claude-opus-5-5", effort="high", stranded=None)
        store.append_checkpoint(SID, "## x\nnote\n")
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, "handoff")])
        self.assertEqual(code, 0, err)
        args, acct = self.started_args()
        self.assertEqual((args[:4], acct), (["--model", "claude-opus-5-5", "--effort", "high"], "b"))
        self.assertEqual(args[4:], [cli._pickup_prompt(store.session(SID), NOW)])
        self.assertIn("Read its checkpoint note", args[4])

    def test_a_request_file_without_a_name_still_reads(self):
        store._requests().mkdir(parents=True, exist_ok=True)
        (store._requests() / "77.json").write_text(json.dumps(
            {"session": SID, "manual": True, "pid": 4321, "kind": "handoff"}))
        self.assertEqual(store.take_request(77), Request(SID, True, 4321, "handoff"))
        store.save_request(78, Request(SID, True, 4321, "handoff", name="x-h2"))
        self.assertEqual(store.take_request(78).name, "x-h2")

    def test_the_launcher_names_the_fresh_session_as_asked(self):
        self.record(model="claude-opus-5-5", effort="high", stranded=None)
        store.append_checkpoint(SID, "## x\nnote\n")
        code, err = self.launches("run", "-a", "a", requests=[
            Request(SID, True, 4321, "handoff", name="work-h2")])
        self.assertEqual(code, 0, err)
        args, _ = self.started_args()
        self.assertEqual(args[4:6], ["--name", "work-h2"])
        self.assertIn("Read its checkpoint note", args[6])

    def test_the_launcher_has_the_note_written_on_the_sessions_own_account(self):
        self.record(model="claude-opus-5-5", effort="high", stranded=None)
        self.snap("a", five=(10, 3), seven=(50, 90))                 # a can work: its cache
        with mock.patch.dict(os.environ, {"FAKE_NOTE": NOTE_JSON}):
            code, err = self.launches("run", "-a", "a", requests=[
                Request(SID, True, 4321, "handoff-note")])
        self.assertEqual(code, 0, err)
        asked = (self.log / "note-a").read_text().splitlines()
        self.assertEqual(asked[:12], ["-p", "--output-format", "stream-json", "--verbose",
                                      "--include-partial-messages", "--tools", "", "--resume",
                                      SID, "--model", "claude-opus-5-5", "--effort"])
        self.assertEqual(asked[12:], ["high", cli.HANDOFF_NOTE_ASK])
        self.assertEqual(Path((self.log / "note-cwd-a").read_text().strip()), self.work.resolve())
        self.assertTrue(self.note().endswith("\nDone: parser. Next: tests.\n"))
        args, _ = self.started_args()
        self.assertNotIn("--resume", args)                           # fresh, reads the note
        self.assertIn("Read its checkpoint note", args[-1])

    def test_on_another_account_when_its_own_is_out(self):
        self.record()
        with mock.patch.dict(os.environ, {"FAKE_NOTE": NOTE_JSON}):
            self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, "handoff-note")])
        self.assertFalse((self.log / "note-a").exists())
        self.assertTrue((self.log / "note-b").exists())

    def check_fallback(self, env, why, wait=None):
        self.record(stranded=None)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, "HANDOFF_WAIT_S", wait or cli.HANDOFF_WAIT_S):
            t = time.monotonic()
            code, err = self.launches("run", "-a", "a", requests=[
                Request(SID, True, 4321, "handoff-note")])
        self.assertEqual(code, 0, err)
        self.assertIn(f"no handoff note ({why}); picking the session up instead", err)
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])    # a plain pickup
        self.assertFalse(store.checkpoint_path(SID).exists())
        return time.monotonic() - t

    def test_a_failed_turn_falls_back_to_a_pickup(self):
        self.check_fallback({"FAKE_NOTE": NOTE_JSON, "FAKE_NOTE_EXIT": "1"},
                            "the turn failed (exit 1)")
        store.move_session(SID, "a")
        self.check_fallback({"FAKE_NOTE": json.dumps({"is_error": True, "result": "x"})},
                            "the answer held no note")

    def test_a_timeout_falls_back_to_a_pickup(self):
        took = self.check_fallback({"FAKE_NOTE": NOTE_JSON, "FAKE_NOTE_SLEEP": "5"},
                                   "no answer in 0.5 s", wait=0.5)
        self.assertLess(took, 4)                                     # the turn was not awaited

    def test_c_skips_to_a_pickup(self):
        with mock.patch.object(cli, "_note_wait", return_value=("skip", None)):
            self.check_fallback({"FAKE_NOTE": NOTE_JSON}, "skipped")

    def test_no_account_can_work(self):
        self.snap("b", five=(100, 3), seven=(50, 90))
        self.snap("c", five=(100, 3), seven=(50, 90))
        self.record(stranded=None)
        code, err = self.launches("run", "-a", "a", requests=[
            Request(SID, True, 4321, "handoff-note")], tty=True, answers=["s"])
        self.assertIn("no handoff note (no account can take work); picking the session up", err)
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])


class Switch(Handover):
    """D5: bare `fairshare` inside a session."""

    def test_without_a_launcher_it_refuses_and_never_nests(self):
        self.record()
        code, _, err = self.inside()
        self.assertEqual(code, 1)
        self.assertIn("/exit, then fairshare", err)
        self.launched.assert_not_called()

    def test_under_a_launcher_it_asks_with_the_flags(self):
        self.record()
        launcher = self.started()
        code, out, _ = self.inside("--model", "sonnet", "-a", "c", launcher=launcher)
        self.assertEqual(code, 0)
        self.assertEqual(launcher.wait(timeout=WAIT), -signal.SIGUSR1)
        self.assertEqual(store.take_request(launcher.pid),
                         Request(SID, True, 4321, "switch", "sonnet", None, "c"))
        self.assertEqual(self.inside("--bogus", launcher=self.started())[0], 2)  # did you mean

    def test_the_launcher_starts_fresh_with_model_and_effort_and_the_resume_line(self):
        self.record(model="claude-opus-5-5", effort="high", stranded=None)
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, "switch")])
        self.assertEqual(code, 0, err)
        self.assertIn(f"to resume the old session: fairshare run -a a -- --resume {SID}", err)
        args, acct = self.started_args()
        self.assertEqual((args, acct), (["--model", "claude-opus-5-5", "--effort", "high"], "b"))
        self.assertEqual(store.session(SID).account, "a")            # the old one is left


class WorkerModel(Handover):
    """D6."""

    def worker(self, *argv):
        os.chdir(self.work)                       # a trusted folder (HOME takes no copy)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "p-1"}):
            return self.go("worker", "--name", f"fs-{argv[-1]}", *argv)

    def test_model_and_effort_are_required(self):
        for argv in (("x",), ("--model", "opus", "x"), ("--effort", "high", "x")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as e, \
                    mock.patch("sys.stderr", io.StringIO()):
                self.worker(*argv)
            self.assertEqual(e.exception.code, 2)

    def test_none_adds_no_effort_argument_and_is_recorded(self):
        code, _, err = self.worker("--model", "haiku", "--effort", "none", "x")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b")[3:5], ["--model", "haiku"])
        self.assertNotIn("--effort", self.bg_args("b"))
        w = store.workers()[0]
        self.assertEqual((w.model, w.effort), ("haiku", "none"))

    def test_the_model_filter_chooses_its_account(self):
        self.opus_spent("b")
        self.assertEqual(self.worker("--model", "opus", "--effort", "high", "x")[0], 0)
        self.assertEqual(store.workers()[0].account, "c")
        self.opus_spent("c")
        code, _, err = self.worker("--model", "opus", "--effort", "high", "y")
        self.assertEqual(code, 1)
        self.assertIn("name another with --model; no worker started", err)
        self.assertEqual(self.worker("--model", "opus", "--effort", "high", "--account", "b",
                                     "z")[0], 0)                       # explicit: no filter


class Skill(unittest.TestCase):
    """D7: the skill maps `/fairshare` to the command and names the worker's model rule."""

    def test_the_skill_says_so_and_never_mentions_carrying_over(self):
        text = claude.skill(Path("/x"))[1]
        for words in ('fairshare worker --name <name> --model <model> --effort <level> "<prompt>"',
                      "Always pass `--name`", "the next `-hN`",
                      "--effort none", "cheaper model", "strongest model",
                      "run `fairshare <args>` exactly as given", "write the note yourself",
                      'fairshare handoff "<note>"', "`! fairshare ...`"):
            self.assertIn(words, text)
        self.assertNotRegex(text.lower(), r"carr(y|ies|ied)\s+(\w+\s+)?over|keeps? (its|the) "
                                          r"model")


class NoteWait(unittest.TestCase):
    """The handoff note's wait on a real pseudo-terminal, with processes this test starts."""

    def setUp(self):
        self.master, slave = pty.openpty()
        self.tty = os.fdopen(slave, "r")
        self.addCleanup(self.tty.close)
        self.addCleanup(os.close, self.master)
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self.before = termios.tcgetattr(slave)

    def wait(self, keys=b"", cmd=("sleep", "5"), seconds=3.0):
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        orig, err = cli.termios.tcflush, io.StringIO()

        def flushed(fd, how):                     # type the keys after the old input went
            orig(fd, how)
            if keys:
                os.write(self.master, keys)
        with mock.patch.object(sys, "stdin", self.tty), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(cli.termios, "tcflush", flushed):
            how, _ = cli._note_wait(proc, claude, "a", cli._Status(True), seconds)
        self.assertEqual(test_pickup.Countdown.attrs(termios.tcgetattr(self.tty.fileno())),
                         test_pickup.Countdown.attrs(self.before))                # restored
        self.assertIn("reading the session on a…", err.getvalue())
        self.assertIn(" · c = skip", err.getvalue())
        self.assertTrue(err.getvalue().endswith("\r\x1b[K"))                   # cleared
        return how

    def test_c_skips_and_other_keys_do_not(self):
        t = time.monotonic()
        self.assertEqual(self.wait(b"xc"), "skip")
        self.assertLess(time.monotonic() - t, 2)

    def test_done_when_the_turn_ends(self):
        self.assertEqual(self.wait(cmd=("true",)), "done")

    def test_timeout(self):
        self.assertEqual(self.wait(seconds=0.4), "timeout")


class MovedPrompt(Fleet):
    """The resumed session's first prompt names a move only when the account changed, and why:
    a limit, a plan window at the first warning level (a move the warning asked for), or a move
    asked for."""
    def setUp(self):
        super().setUp()
        self.add("a")
        self.add("b")

    def test_another_account_is_a_move(self):
        rec = SessionRecord(SID, "a", "/w", None, NOW)
        self.assertEqual(cli._moved_prompt(rec, "b"), MOVED.format(new="b", why=ASKED))

    def test_a_window_at_the_warning_level_is_near_the_limit(self):
        rec = SessionRecord(SID, "a", "/w", None, NOW)
        for five, seven, why in (((90, 1), (10, 50), NEAR), ((10, 1), (95, 50), NEAR),
                                 ((89, 1), (89, 50), ASKED)):
            with self.subTest(five=five, seven=seven):
                self.snap("a", five=five, seven=seven)
                self.assertEqual(cli._moved_prompt(rec, "b"), MOVED.format(new="b", why=why))

    def test_a_window_reset_since_or_a_model_week_is_not_near(self):
        rec = SessionRecord(SID, "a", "/w", None, NOW)
        store.save("a", Snapshot(NOW, {"five_hour": Window(97, NOW - 60),
                                       "seven_day": Window(20, NOW + 50 * H),
                                       "seven_day:Opus": Window(99, NOW + 30 * H)}))
        self.assertEqual(cli._moved_prompt(rec, "b"), MOVED.format(new="b", why=ASKED))

    def test_a_limit_comes_first(self):
        rec = SessionRecord(SID, "a", "/w", None, NOW, stranded_at=NOW)
        self.snap("a", five=(100, 1), seven=(10, 50))
        self.assertEqual(cli._moved_prompt(rec, "b"), MOVED.format(new="b", why=LIMIT))

    def test_the_same_account_is_a_restart(self):
        rec = SessionRecord(SID, "a", "/w", None, NOW, stranded_at=NOW)
        self.assertEqual(cli._moved_prompt(rec, "a"),
                         f"fairshare restarted this session on account a because {LIMIT}; it is "
                         "still the best account. Continue where you left off.")


if __name__ == "__main__":
    unittest.main()
