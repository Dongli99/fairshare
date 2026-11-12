"""The handoff note's progress: the adapter reads the note turn's stream (the recorded
tests/fixtures/claude-2.1.322/note-stream.jsonl) into provider-neutral events; the launcher's
wait drains the pipe and shows the phase on one status line (a real pseudo-terminal, or none);
the launcher's first line after it stopped its child. Fake processes are Python one-liners this
test starts."""
import io
import json
import os
import pty
import subprocess
import sys
import termios
import time
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import NOTE_THINKING, NOTE_WRITING, Request
from fairshare.providers import claude

import test_pickup
from test_handover import Handover
from test_pickup import SID

STREAM = Path(__file__).parent / "fixtures/claude-2.1.322/note-stream.jsonl"
LINES = STREAM.read_text().splitlines()
RESULT = json.loads(LINES[-1])["result"]
# A Haiku turn at low effort: no thinking block, the text at once.
HAIKU = [json.dumps(x) for x in (
    {"type": "system", "subtype": "init"},
    {"type": "stream_event", "event": {"type": "message_start"}},
    {"type": "stream_event", "event": {"type": "content_block_start",
                                       "content_block": {"type": "text", "text": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta",
                                       "delta": {"type": "text_delta", "text": "Done: x."}}},
    {"type": "stream_event", "event": {"type": "content_block_delta",
                                       "delta": {"type": "text_delta", "text": " Next: y."}}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "Done: x. Next: y."})]
# Writes the lines in argv[1] (a JSON list), sleeping `pause` seconds first and before the first
# thinking and text blocks, so each phase is seen on its own; then exits.
WRITER = """import json, sys, time
lines, pause = json.loads(open(sys.argv[1]).read()), float(sys.argv[2])
time.sleep(pause)
for l in lines:
    if '"content_block_start"' in l:
        time.sleep(pause)
    sys.stdout.write(l + "\\n"); sys.stdout.flush()
"""


class Utf8(io.StringIO):
    encoding = "utf-8"


def phases(lines):
    """The phase order and the written text the events of `lines` give."""
    seen, text = ["reading"], ""
    for ev in filter(None, map(claude.note_event, lines)):
        p = {NOTE_THINKING: "thinking", NOTE_WRITING: "writing"}[ev.kind]
        if p != seen[-1] and not (seen[-1] == "writing"):
            seen.append(p)
        text += ev.text
    return seen, text


class Adapter(unittest.TestCase):
    def test_the_fixture_reads_thinking_then_writing_with_its_words_and_result(self):
        seen, text = phases(LINES)
        self.assertEqual(seen, ["reading", "thinking", "writing"])
        self.assertEqual(len(text.split()), len(RESULT.split()))
        self.assertEqual(text.strip(), RESULT.strip())
        self.assertEqual(claude.note_text(LINES[-1]), RESULT.strip())

    def test_no_thinking_block_writes_at_once(self):
        seen, text = phases(HAIKU)
        self.assertEqual((seen, text), (["reading", "writing"], "Done: x. Next: y."))
        self.assertEqual(claude.note_text(HAIKU[-1]), "Done: x. Next: y.")

    def test_lines_that_say_nothing_or_do_not_parse_are_none(self):
        for line in ("", "not json", "[1]", '{"type": "stream_event", "event": "x"}',
                     '{"type": "stream_event", "event": {"type": "content_block_delta", '
                     '"delta": {"type": "new_kind"}}}', '{"type": "system", "subtype": "init"}',
                     LINES[-1], '{"type": "stream_event", "event": {"type": "message_start"}}'):
            with self.subTest(line=line):
                self.assertIsNone(claude.note_event(line))

    def test_the_turn_streams(self):
        self.assertEqual(claude.NOTE_ARGS[:5], ("-p", "--output-format", "stream-json",
                                               "--verbose", "--include-partial-messages"))


class Wait(unittest.TestCase):
    """`_note_wait` with no terminal: plain phase lines."""

    def fake(self, lines, pause=0.0, script=WRITER):
        data = Path(self.tmp) / "lines.json"
        data.write_text(json.dumps(lines))
        proc = subprocess.Popen([sys.executable, "-c", script, str(data), str(pause)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(),
                                 proc.stdout.close()))
        return proc

    def setUp(self):
        import tempfile
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def wait(self, proc, seconds=10.0):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            got = cli._note_wait(proc, claude, "acct-a", cli._Status(False), seconds)
        return got, err.getvalue()

    def test_the_phases_of_the_fixture_as_plain_lines(self):
        (how, text), err = self.wait(self.fake(LINES, 0.2))
        self.assertEqual((how, text), ("done", RESULT.strip()))
        self.assertEqual(err.splitlines(), ["reading the session on acct-a…", "thinking…",
                                            "writing the note"])
        self.assertNotIn("\r", err)

    def test_no_thinking(self):
        (how, text), err = self.wait(self.fake(HAIKU, 0.2))
        self.assertEqual(text, "Done: x. Next: y.")
        self.assertEqual(err.splitlines(), ["reading the session on acct-a…", "writing the note"])

    def test_a_later_thinking_block_never_goes_back(self):
        later = [LINES[15], LINES[16], LINES[3], LINES[4], HAIKU[-1]]   # text, then thinking
        (_, text), err = self.wait(self.fake(later, 0.1))
        self.assertEqual(err.splitlines(), ["reading the session on acct-a…", "writing the note"])

    def test_lines_that_stop_parsing_keep_the_phase_and_only_the_result_counts(self):
        broken = [*LINES[:6], "{garbage", '{"type": "new"}', LINES[-1]]
        (how, text), err = self.wait(self.fake(broken, 0.1))
        self.assertEqual((how, text), ("done", RESULT.strip()))
        self.assertEqual(err.splitlines(), ["reading the session on acct-a…", "thinking…"])

    def test_no_result_is_no_text(self):
        self.assertEqual(self.wait(self.fake(LINES[:-1]))[0], ("done", None))
        self.assertEqual(self.wait(self.fake([]))[0], ("done", None))

    def test_the_pipe_is_drained(self):
        big = ['{"type": "stream_event", "event": {"type": "ping", "pad": "%s"}}' % ("x" * 1000)
               ] * 300 + [LINES[-1]]                                # about 300 KB
        t = time.monotonic()
        (how, text), _ = self.wait(self.fake(big), seconds=10)
        self.assertEqual((how, text), ("done", RESULT.strip()))
        self.assertLess(time.monotonic() - t, 5)

    def test_a_line_split_across_reads(self):
        script = WRITER.replace('sys.stdout.write(l + "\\n")',
                                'sys.stdout.write(l[:9]); sys.stdout.flush(); time.sleep(0.05); '
                                'sys.stdout.write(l[9:] + "\\n")')
        (how, text), _ = self.wait(self.fake(HAIKU, 0, script))
        self.assertEqual(text, "Done: x. Next: y.")

    def test_timeout(self):
        how, err = self.wait(self.fake(LINES, 5), seconds=0.4)
        self.assertEqual(how, ("timeout", None))


class TerminalWait(unittest.TestCase):
    """`_note_wait` on a real pseudo-terminal."""

    def setUp(self):
        self.master, slave = pty.openpty()
        self.tty = os.fdopen(slave, "r")
        self.addCleanup(self.tty.close)
        self.addCleanup(os.close, self.master)
        self.before = termios.tcgetattr(slave)

    def wait(self, cmd, status=None, prov=claude, seconds=3.0):
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(),
                                 proc.stdout.close()))
        self.err = Utf8()
        with mock.patch.object(sys, "stdin", self.tty), mock.patch.object(sys, "stderr", self.err):
            try:
                return cli._note_wait(proc, prov, "a", status or cli._Status(True), seconds)
            finally:
                self.assertEqual(test_pickup.Countdown.attrs(termios.tcgetattr(self.tty.fileno())),
                                 test_pickup.Countdown.attrs(self.before))        # restored

    def frames(self):
        return [x for x in self.err.getvalue().split("\r") if x and x != "\x1b[K"]

    def test_spinner_and_clock_move_with_no_output(self):
        status = cli._Status(True)
        status.start -= 59.5                                         # a minute in, nearly
        t = time.monotonic()
        self.assertEqual(self.wait(("sleep", "1"), status), ("done", None))
        took = time.monotonic() - t
        frames = self.frames()
        spins = [f[0] for f in frames]
        self.assertGreaterEqual(len(frames), 5 * took * 0.8)
        self.assertGreaterEqual(len(set(spins)), 5)
        self.assertTrue(set(spins) <= set(cli.SPIN_UTF8))            # UTF-8: braille
        self.assertIn(" 0:59 · c = skip", frames[0])
        self.assertIn(" 1:00 · c = skip", frames[-1])
        self.assertTrue(all(f.endswith("\x1b[K") for f in frames))

    def test_the_phases_are_redrawn_in_place(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "l.json").write_text(json.dumps(LINES))
            got = self.wait((sys.executable, "-c", WRITER, str(Path(d) / "l.json"), "0.2"))
        self.assertEqual(got, ("done", RESULT.strip()))
        shown = self.frames()
        order = [next(i for i, f in enumerate(shown) if want in f)
                 for want in ("reading the session on a…", "thinking…", "writing the note · ")]
        self.assertEqual(order, sorted(order))
        self.assertIn(f"writing the note · {len(RESULT.split())} words", shown[-2])
        self.assertTrue(self.err.getvalue().endswith("\r\x1b[K"))   # cleared at the end

    def test_ascii_spinner_and_the_width(self):
        status = cli._Status(True)
        status.frames = cli.SPIN_ASCII
        with mock.patch.object(cli, "_width", return_value=30):
            self.wait(("sleep", "0.5"), status)
        for f in self.frames():
            self.assertLessEqual(len(f.removesuffix("\x1b[K")), 29)
            self.assertIn(f[0], "|/-\\")

    def test_ascii_when_stderr_is_not_utf8(self):
        with mock.patch.object(sys, "stderr", io.StringIO()):          # encoding None
            self.assertEqual(cli._Status(True).frames, cli.SPIN_ASCII)

    def test_terminal_mode_restored_on_exception(self):
        class Broken:
            @staticmethod
            def note_event(line):
                raise RuntimeError("boom")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "l.json").write_text(json.dumps(HAIKU))
            with self.assertRaises(RuntimeError):
                self.wait((sys.executable, "-c", WRITER, str(Path(d) / "l.json"), "0"),
                          prov=Broken)                             # restored: checked in wait


class Notice(Handover):
    """The launcher's first line, and the note's steps in order, through `fairshare run`."""

    def test_the_header_comes_before_the_survey_then_the_steps(self):
        self.record(model="claude-opus-5-5", effort="high", stranded=None)
        self.snap("a", five=(10, 3), seven=(50, 90))
        seen, real = [], cli._survey

        def survey():
            seen.append(sys.stderr.getvalue())
            return real()
        with mock.patch.dict(os.environ, {"FAKE_NOTE": STREAM.read_text()}), \
                mock.patch.object(cli, "_survey", survey):
            code, err = self.launches("run", "-a", "a", requests=[
                Request(SID, True, 4321, "handoff-note")])
        self.assertEqual(code, 0, err)
        header = f"fairshare · handing off {SID[:8]} (Claude-Opus-5-5 · high)\n"
        self.assertTrue(seen[0].endswith(header + "checking accounts…\n"), seen[0])
        path = store.checkpoint_path(SID)
        tail = err[err.index(header):].splitlines()
        self.assertEqual(tail[:5], [header.strip(), "checking accounts…",   # the fake writes
                                    "reading the session on a…", "writing the note",  # at once
                                    f"✓ note saved, {len(RESULT.split())} words → {path}"])
        self.assertTrue(any(x.startswith("fairshare -> b") for x in tail[5:]))  # the launch line
        self.assertEqual(err.count("fairshare -> "), 2)
        self.assertNotIn("\r", err)
        self.assertIn(RESULT.strip(), path.read_text())

    def test_a_missing_result_is_a_pickup(self):
        self.record(stranded=None)
        with mock.patch.dict(os.environ, {"FAKE_NOTE": "\n".join(LINES[:-1])}):
            code, err = self.launches("run", "-a", "a", requests=[
                Request(SID, True, 4321, "handoff-note")])
        self.assertIn("no handoff note (the answer held no note); picking the session up", err)
        self.assertEqual(self.started_args()[0][:2], ["--resume", SID])

    def test_every_other_kind_says_one_line_at_once(self):
        for kind, line in (("switch", f"fairshare · switching from {SID[:8]} (Opus · high)…"),
                           ("handoff", f"fairshare · handing off {SID[:8]} (Opus · high)…"),
                           ("pickup", f"fairshare · picking up {SID[:8]} (Opus · high)…")):
            with self.subTest(kind=kind):
                self.record(model="claude-opus-5-5", effort="high", stranded=None, name="Opus")
                store.move_session(SID, "a")
                store.append_checkpoint(SID, "## x\nnote\n")
                code, err = self.launches("run", "-a", "a", requests=[
                    Request(SID, True, 4321, kind)])
                self.assertEqual(code, 0, err)
                lines = err.splitlines()
                at = lines.index(line)
                self.assertTrue(lines[at + 1:] and not any(line in x for x in lines[at + 1:]))
                self.assertEqual(err.count("fairshare -> "), 2)

    def test_no_model_known_gives_the_id_alone(self):
        self.record(stranded=None)
        code, err = self.launches("run", "-a", "a", requests=[Request(SID, True, 4321, "switch")])
        self.assertIn(f"fairshare · switching from {SID[:8]}…\n", err)


if __name__ == "__main__":
    unittest.main()
