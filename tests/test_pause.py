"""A launch with no pause, the key reader and the transcript's `!` commands. Same fleet as
test_honest (temp HOME, config, state and hub; fakes first on PATH; inherited session variables
cleared; launches recorded, not made). Launches run in HOME, where no trust question is asked."""
import json
import os
import pty
import termios
import io
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import SessionSeen, Tuning, load_accounts

from test_honest import NOW, ORG, Fleet


class Launch(Fleet):
    """A launch starts on the top account at once (no pause, decision 36); close accounts are
    still spread by their live sessions."""
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")

    def start(self, *argv, tty=True):
        with mock.patch.object(cli, "_read_key") as read_key:
            code, out, err = self.go("run", *argv, tty=tty)
        read_key.assert_not_called()
        return code, err

    def launched_id(self):
        return Path(self.launched_dir()).name

    def test_a_close_second_starts_the_top_one_at_once(self):
        self.snap("a", five=(10, 2), seven=(10, 50))           # score 1.8
        self.snap("b", five=(10, 2), seven=(15, 50))           # 1.7: within 10%
        code, err = self.start()
        self.assertEqual((code, self.launched_id()), (0, "a"))
        self.assertNotIn("starting a in", err)

    def test_a_close_call_goes_to_the_account_with_fewer_live_sessions(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(15, 50))
        store.note_session(SessionSeen("s1"), "a", NOW)        # a session just started on a
        self.start()
        self.assertEqual(self.launched_id(), "b")

    def test_a_session_not_seen_lately_does_not_count(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(15, 50))
        store.note_session(SessionSeen("s1"), "a", NOW - cli.LIVE_S - 1)
        self.start()
        self.assertEqual(self.launched_id(), "a")

    def test_an_old_launch_pause_or_warning_move_setting_is_ignored(self):
        cfg = self.tmp / "accounts.toml"
        cfg.write_text(cfg.read_text().replace(
            "\n\n", '\n\n[policy]\nlaunch_pause = 5\nwarning_move = "pickup"\n\n', 1))
        self.assertEqual(load_accounts(cfg)[1], Tuning())
        code, out, _ = self.go("doctor")
        self.assertIn("warn  accounts.toml: [policy] launch_pause, warning_move no longer used; "
                      "you may remove them", out)


class ReadKey(__import__("unittest").TestCase):
    """The key reader on a real pseudo-terminal: a digit and q (Enter, c and the timeout are
    covered by test_pickup's Countdown)."""

    def setUp(self):
        self.master, slave = pty.openpty()
        self.tty = os.fdopen(slave, "r")
        self.addCleanup(self.tty.close)
        self.addCleanup(os.close, self.master)
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

    def read(self, keys):
        orig = cli.termios.tcflush

        def flushed(fd, how):
            orig(fd, how)
            os.write(self.master, keys)
        with mock.patch.object(cli.sys, "stdin", self.tty), \
                mock.patch.object(cli.sys, "stderr", io.StringIO()), \
                mock.patch.object(cli.termios, "tcflush", flushed):
            return cli._read_key(lambda n: f"{n}", 2, b"12q")

    def test_a_digit_and_q_upper_case_too(self):
        self.assertEqual(self.read(b"x2"), b"2")
        self.assertEqual(self.read(b"Q"), b"q")


class Transcript(Fleet):
    def test_a_bang_command_and_its_output_are_shortened(self):
        out_lines = "\n".join(f"line {i}" for i in range(1, 26))
        records = [
            {"type": "user", "isSidechain": False, "message": {"role": "user",
                                                                "content": "Check the tree."}},
            {"type": "user", "isSidechain": False, "message": {
                "role": "user", "content": "<bash-input>ls -la</bash-input>"}},
            {"type": "user", "isSidechain": False, "message": {"role": "user", "content":
                f"<bash-stdout>{out_lines}</bash-stdout><bash-stderr></bash-stderr>"}},
            {"type": "user", "isSidechain": False, "message": {"role": "user", "content":
                "<bash-stdout></bash-stdout><bash-stderr>ls: x: No such file</bash-stderr>"}},
            {"type": "assistant", "isSidechain": False, "message": {
                "role": "assistant", "content": [{"type": "text", "text": "Seen."}]}}]
        path = self.tmp / "t.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        store.note_session(SessionSeen("s1", str(self.tmp), str(path)), "a", NOW)
        code, out, _ = self.go("transcript", "s1")
        self.assertEqual(code, 0)
        self.assertEqual(out, "user: Check the tree.\n\nuser ran: ls -la\n\noutput:\n"
                         + "\n".join(f"line {i}" for i in range(1, 21))
                         + "\n… (5 more lines)\n\noutput:\nls: x: No such file\n\n"
                           "assistant: Seen.\n")
