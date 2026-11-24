"""Stage 5b, part F: the launch pause and the transcript's `!` commands. Same fleet as
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
from fairshare.model import ConfigError, SessionSeen, Snapshot, Window, load_accounts

from test_honest import H, NOW, ORG, Fleet
from test_rates import rated, spending


class Pause(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.keys, self.texts = [], []

    def run_with(self, key=None, *argv, tty=True):
        def read_key(text, seconds, keys):
            self.keys.append((seconds, keys))
            self.texts.append(text(seconds))
            return key
        with mock.patch.object(cli, "_read_key", read_key):
            code, out, err = self.go("run", *argv, tty=tty)
        return code, err

    def launched_id(self):
        return Path(self.launched_dir()).name

    def test_no_trigger_no_pause(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(60, 50))
        self.run_with()
        self.assertEqual((self.keys, self.launched_id()), ([], "a"))

    def test_a_close_second_pauses_and_shows_the_choice(self):
        self.snap("a", five=(10, 2), seven=(10, 50))           # score 1.8
        self.snap("b", five=(10, 2), seven=(15, 50))           # 1.7: within 10%
        code, err = self.run_with()
        self.assertEqual(self.keys, [(3, b"12q")])
        self.assertEqual(self.texts, ["starting a in 3 s · Enter = now · 1-2 = that account · "
                                      "q = quit"])
        self.assertIn("  1 > a            seven_day 10% used, resets in 50h", err)
        self.assertIn("  2   b            seven_day 15% used, resets in 50h", err)
        self.assertEqual(self.launched_id(), "a")              # the time ran out: the top one

    def test_a_close_call_goes_to_the_account_with_fewer_live_sessions(self):
        self.snap("a", five=(10, 2), seven=(10, 50))           # score 1.8
        self.snap("b", five=(10, 2), seven=(15, 50))           # 1.7: within 10%
        store.note_session(SessionSeen("s1"), "a", NOW)        # a session just started on a
        _, err = self.run_with()
        self.assertEqual(self.texts, ["starting b in 3 s · Enter = now · 1-2 = that account · "
                                      "q = quit"])
        self.assertIn("  1 > b ", err)
        self.assertEqual(self.launched_id(), "b")

    def test_a_session_not_seen_lately_does_not_count(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(15, 50))
        store.note_session(SessionSeen("s1"), "a", NOW - cli.LIVE_S - 1)
        self.run_with()
        self.assertEqual(self.launched_id(), "a")

    def test_each_tight_trigger(self):
        # b: eligible far behind; for the grace hour, b cannot work (else the grace does not hold)
        rated(self.tmp, "a")                                   # $1.00 a point of its week
        spending("s-a", "a", 4.0)                               # $4.00 this hour
        cases = {"under an hour to its limit": (dict(five=(10, 2), seven=(97, 10)), (97, 100)),
                 "hot short window (inside its grace hour)": (dict(five=(90, 0.5), seven=(10, 10)),
                                                              (99, 100))}
        for label, (top, other_week) in cases.items():
            with self.subTest(label):
                self.keys.clear()
                self.snap("a", **top)
                self.snap("b", five=(10, 2), seven=other_week)
                self.run_with()
                self.assertEqual(len(self.keys), 1)
                self.assertEqual(self.launched_id(), "a")

    def test_a_full_week_idle_or_without_a_rate_does_not_pause(self):
        self.snap("a", five=(10, 2), seven=(97, 10))
        self.snap("b", five=(10, 2), seven=(97, 100))
        self.run_with()                                         # no rate
        rated(self.tmp, "a")
        self.run_with()                                         # a rate, nothing spent
        self.assertEqual(self.keys, [])
        self.assertEqual(self.launched_id(), "a")

    def test_a_spent_model_week_pauses(self):
        store.save("a", Snapshot(NOW, {"seven_day": Window(10, NOW + 50 * H),
                                       "seven_day:Fable": Window(99, NOW + 50 * H)}))
        self.snap("b", five=(10, 2), seven=(80, 50))
        _, err = self.run_with()
        self.assertIn("a's Fable week is spent", err)
        self.assertEqual(len(self.keys), 1)

    def test_a_warning_line_is_a_trigger(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(60, 50))
        s = cli._survey()
        self.assertFalse(cli._worth_pausing(s, s.verdicts[0], False))
        self.assertTrue(cli._worth_pausing(s, s.verdicts[0], True))

    def test_keys(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(15, 50))
        for key, account in ((b"\n", "a"), (b"2", "b"), (b"1", "a"), (None, "a")):
            with self.subTest(key=key):
                self.run_with(key)
                self.assertEqual(self.launched_id(), account)
        code, err = self.run_with(b"q")
        self.assertEqual(code, 0)
        self.launched.assert_not_called()
        self.assertIn("not started", err)

    def test_off_no_terminal_run_a(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(15, 50))
        self.run_with(tty=False)
        self.run_with(None, "-a", "b")
        self.assertEqual(self.keys, [])
        cfg = self.tmp / "accounts.toml"
        cfg.write_text(cfg.read_text().replace("\n\n", "\n\n[policy]\nlaunch_pause = 0\n\n", 1))
        self.assertEqual(load_accounts(cfg)[1].launch_pause, 0)
        self.run_with()
        self.assertEqual(self.keys, [])

    def test_a_negative_pause_is_refused(self):
        cfg = self.tmp / "accounts.toml"
        cfg.write_text('hub = "/h"\n[policy]\nlaunch_pause = -1\n')
        with self.assertRaises(ConfigError):
            load_accounts(cfg)

    def test_a_pickup_does_not_pause(self):
        self.snap("a", five=(10, 2), seven=(10, 50))
        self.snap("b", five=(10, 2), seven=(15, 50))
        store.note_session(SessionSeen("s1", str(self.tmp / "home"), "/t.jsonl"), "a", NOW)
        with mock.patch.object(cli, "_read_key") as read_key:
            self.go("pickup", "s1", tty=True)
        read_key.assert_not_called()


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
