"""Stage 5a: the session map, fairshare's own hook and its wiring, fresher usage, pickup and the
transcript. Same temp HOME, config, state and hub as test_hub and test_honest (their setUp
proves the fakes come first on PATH and clears any FAIRSHARE_* and CLAUDE* variable a
surrounding session or launcher set). The only transcript read is the planner's synthetic
fixture."""
import io
import json
import os
import pty
import signal
import termios
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, hub, session, store
from fairshare.model import ConfigError, Request, SessionSeen, Snapshot, Window, load_accounts
from fairshare.providers import claude

from test_honest import FIXTURES, H, NOW, Fleet
from test_hub import Base, write
from test_session import SRC, WAIT, Supervised, until

T0 = 1_900_000_000.0
SID = "0a1b2c3d-0000-4000-8000-000000000001"


class Sessions(Base):
    """Capture records the session it runs in."""

    def setUp(self):
        super().setUp()
        write(self.cfg, f'hub = "{self.hub}"\n\n[[account]]\nid = "a"\nplan = "claude-pro"\n')
        self.clock = [T0]
        p = mock.patch.object(cli, "_now", lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)

    def capture(self, sid=SID, cwd="/work/demo", transcript="/t/x.jsonl", account="a", **extra):
        payload = {"session_id": sid, "cwd": cwd, "transcript_path": transcript, **extra}
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(
                json.dumps(payload).encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(hub.account_dir(self.hub,
                                                                                 account))}):
            return self.go("capture")

    def file(self, sid=SID):
        return self.tmp / "state" / "sessions" / f"{sid}.json"

    def test_capture_writes_the_record(self):
        self.assertEqual(self.capture(), 0)
        self.assertEqual(json.loads(self.file().read_text()),
                         {"account": "a", "cwd": "/work/demo", "transcript": "/t/x.jsonl",
                          "seen": T0, "stranded_at": None})
        self.assertEqual(store.session(SID).account, "a")

    def test_an_unchanged_record_is_rewritten_at_most_once_a_minute(self):
        self.capture()
        self.clock[0] += 59
        self.capture()
        self.assertEqual(store.session(SID).seen, T0)             # throttled: no write
        self.clock[0] += 1
        self.capture()
        self.assertEqual(store.session(SID).seen, T0 + 60)

    def test_a_change_is_written_at_once(self):
        self.capture()
        self.clock[0] += 5
        self.capture(transcript="/t/y.jsonl")
        r = store.session(SID)
        self.assertEqual((r.transcript, r.seen), ("/t/y.jsonl", T0 + 5))

    def test_the_folder_is_the_one_it_started_in(self):
        self.capture()
        self.clock[0] += 61
        self.capture(cwd="/work/demo/worktree")          # its shell moved; the session did not
        r = store.session(SID)
        self.assertEqual((r.cwd, r.seen), ("/work/demo", T0 + 61))

    def test_a_field_left_out_keeps_its_value(self):
        self.capture()
        self.clock[0] += 61
        self.capture(cwd=None, transcript=None)
        r = store.session(SID)
        self.assertEqual((r.cwd, r.transcript, r.seen), ("/work/demo", "/t/x.jsonl", T0 + 61))

    def test_records_unseen_for_seven_days_are_dropped_when_writing(self):
        self.capture(sid="old")
        self.clock[0] += 3 * 24 * 3600
        self.capture(sid="middle")
        self.clock[0] += 4 * 24 * 3600 + 1
        self.capture(sid="new")
        self.assertEqual([r.id for r in store.sessions()], ["middle", "new"])

    def test_outside_the_hub_or_without_an_id_nothing_is_recorded(self):
        self.capture(account="elsewhere")
        self.capture(sid="")
        self.capture(sid="../escape")
        self.assertEqual(store.sessions(), [])

    def test_the_line_still_prints(self):
        self.capture(rate_limits={"seven_day": {"used_percentage": 40, "resets_at": 4e9}})
        self.assertEqual(self.out, "a · 5h - · 7d 40%\n")

    def week(self, sid, pct):
        self.clock[0] += 1
        self.capture(sid=sid, rate_limits={"seven_day": {"used_percentage": pct,
                                                         "resets_at": 4e9}})
        return store.load("a").windows["seven_day"].used_pct

    def test_a_session_redrawing_what_it_drew_last_says_nothing_new(self):
        """An idle session open across a credit-back keeps redrawing its old number; once a
        probe has the lower one, that redraw must not put the old number back."""
        self.assertEqual(self.week("idle", 82), 82)               # its first draw counts
        store.record("a", {"seven_day": Window(34, 4e9)}, self.clock[0], probe=True)
        self.assertEqual(self.week("idle", 82), 34)               # unchanged since: ignored
        self.assertEqual(store.session("idle").drawn, {"a": {"seven_day": [82, 4e9]}})
        self.assertEqual(self.week("active", 36), 36)             # another session's news
        self.assertEqual(self.week("idle", 83), 83)               # its own number moved: news

    def test_a_session_moved_to_another_account_draws_anew_there(self):
        seen = SessionSeen("s")
        self.assertEqual(store.drawn_news(seen, "a", {"seven_day": Window(50, 4e9)}, T0),
                         {"seven_day": Window(50, 4e9)})
        self.assertEqual(store.drawn_news(seen, "a", {"seven_day": Window(50, 4e9)}, T0), {})
        store.move_session("s", "b")
        self.assertEqual(store.drawn_news(seen, "b", {"seven_day": Window(50, 4e9)}, T0),
                         {"seven_day": Window(50, 4e9)})


class Wiring(unittest.TestCase):
    """fairshare's StopFailure hook and the statusLine's refresh, in the hub's settings."""
    MINE = ('{\n  "statusLine": {"type": "command", "command": "/bin/fairshare capture"},\n'
            '  "hooks": {\n    "StopFailure": [\n      {\n        "matcher": "rate_limit",\n'
            '        "hooks": [{"type": "command", "command": "~/notify.sh"}]\n      }\n    ],\n'
            '    "Stop": [{"hooks": [{"type": "command", "command": "~/stop.sh"}]}]\n  },\n'
            '  "theme": "dark"\n}\n')

    def test_added_beside_the_users_own_stop_hooks_keeping_every_other_byte(self):
        new = claude.wire(self.MINE, "/bin/fairshare")
        self.assertEqual(new, self.MINE.replace(
            '"/bin/fairshare capture"}', '"/bin/fairshare capture", "refreshInterval": 60}'
        ).replace('"~/notify.sh"}]\n      }\n', '"~/notify.sh"}]\n      },\n      {"matcher": '
                  '"rate_limit", "hooks": [{"type": "command", "command": "/bin/fairshare hook '
                  'stop-failure"}]}\n').replace(
            '"~/stop.sh"}]}]\n', '"~/stop.sh"}]},{"hooks": [{"type": "command", "command": '
            '"/bin/fairshare hook stop"}]}],\n    "PostToolUse": [{"matcher": "*", "hooks": '
            '[{"type": "command", "command": "/bin/fairshare hook post-tool"}]}],\n    '
            '"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/bin/fairshare hook '
            'prompt"}]}]\n'))
        self.assertEqual(claude.hooked(new), ("/bin/fairshare",))
        self.assertIsNone(claude.hooked(self.MINE))

    def test_idempotent(self):
        new = claude.wire(self.MINE, "/bin/fairshare")
        self.assertIsNone(claude.wire(new, "/bin/fairshare"))

    def test_an_older_command_is_changed_in_place(self):
        new = claude.wire(self.MINE, "/bin/fairshare")
        moved = claude.wire(new, "/new/fairshare")
        self.assertEqual(moved, new.replace("/bin/fairshare", "/new/fairshare"))
        self.assertEqual(claude.hooked(moved), ("/new/fairshare",))

    def test_a_wrong_matcher_on_ours_is_set_back(self):
        new = claude.wire(self.MINE, "/bin/fairshare")
        bent = new.replace('{"matcher": "rate_limit", "hooks": [{"type": "command", '
                           '"command": "/bin/fairshare hook', '{"matcher": "unknown", "hooks": '
                           '[{"type": "command", "command": "/bin/fairshare hook')
        self.assertIsNone(claude.hooked(bent))
        self.assertEqual(claude.wire(bent, "/bin/fairshare"), new)

    def test_the_users_own_refresh_interval_is_kept(self):
        mine = self.MINE.replace('capture"}', 'capture", "refreshInterval": 5}')
        self.assertEqual(json.loads(claude.wire(mine, "/bin/fairshare"))["statusLine"]
                         ["refreshInterval"], 5)

    def test_an_unexpected_layout_is_refused(self):
        for bad in ('{"hooks": []}', '{"hooks": {"StopFailure": {}}}'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                claude.wire(bad, "/bin/fairshare")

    def test_hook_input_names_the_session_only_for_its_own_error(self):
        raw = {"session_id": SID, "transcript_path": "/t/x.jsonl", "cwd": "/w",
               "hook_event_name": "StopFailure", "error": "rate_limit"}
        self.assertEqual(claude.hook_session("stop-failure", raw),
                         SessionSeen(SID, "/w", "/t/x.jsonl"))
        self.assertIsNone(claude.hook_session("stop-failure", {**raw, "error": "overloaded"}))
        self.assertIsNone(claude.hook_session("other", raw))


class HookWiredInHub(Base):
    def setUp(self):
        super().setUp()
        write(self.cfg, f'hub = "{self.hub}"\n\n[[account]]\nid = "a"\nplan = "claude-pro"\n')
        hub.check(self.hub, "a", claude)
        self.settings = hub.shared_dir(self.hub) / "settings.json"

    def doctor_line(self):
        self.go("doctor")
        return next(l for l in self.out.splitlines() if "hooks in the hub" in l)

    def test_doctor_fails_without_it_and_fix_adds_it_with_its_diff(self):
        write(self.settings, json.dumps({"statusLine": {
            "type": "command", "command": f"{self.bin}/fairshare capture"}}))
        self.assertEqual(self.doctor_line(), "FAIL  claude: hooks in the hub (stop-failure, "
                                             "post-tool, prompt, stop) do not all call fairshare "
                                             "hook (run `fairshare doctor --fix`)")
        self.go("doctor", "--fix")
        for name in ("stop-failure", "post-tool", "prompt", "stop"):
            self.assertIn(f'+            "command": "{self.bin}/fairshare hook {name}"', self.out)
        self.assertEqual(self.doctor_line(), "pass  claude: hooks in the hub (stop-failure, "
                                             "post-tool, prompt, stop) call fairshare hook")
        self.assertEqual(claude.hooked(self.settings.read_text()), (f"{self.bin}/fairshare",))

    def test_the_hook_command_says_nothing_and_exits_0_whatever_it_gets(self):
        for stdin in (b"", b"not json", b"{}", json.dumps({"session_id": SID}).encode()):
            for name in ("stop-failure", "no-such-hook"):
                with self.subTest(stdin=stdin, name=name), \
                        mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(stdin))):
                    self.assertEqual(self.go("hook", name), 0)
                    self.assertEqual((self.out, self.err), ("", ""))


class TruthfulTime(unittest.TestCase):
    """captured_at says when the numbers were last known to be current."""
    OLD = Snapshot(T0, {"five_hour": Window(20, T0 + 3 * H), "seven_day": Window(40, T0 + 90 * H)})

    def test_an_equal_redraw_leaves_it(self):
        self.assertEqual(store.merge(self.OLD, dict(self.OLD.windows), T0 + 600), self.OLD)
        self.assertEqual(store.merge(self.OLD, {}, T0 + 600), self.OLD)

    def test_a_change_moves_it(self):
        for fresh in ({"five_hour": Window(21, T0 + 3 * H)},          # value
                      {"five_hour": Window(20, T0 + 3 * H + 60)},     # reset
                      {"seven_day:Fable": Window(5, T0 + 90 * H)}):   # a window appeared
            with self.subTest(fresh=fresh):
                self.assertEqual(store.merge(self.OLD, fresh, T0 + 600).captured_at, T0 + 600)

    def test_a_probe_moves_it_even_when_equal(self):
        self.assertEqual(store.merge(self.OLD, dict(self.OLD.windows), T0 + 600,
                                     probe=True).captured_at, T0 + 600)

    def test_a_lower_stale_value_is_still_refused(self):
        snap = store.merge(self.OLD, {"seven_day": Window(30, T0 + 90 * H)}, T0 + 600)
        self.assertEqual(snap, self.OLD)


class RefreshMinutes(unittest.TestCase):
    def load(self, policy):
        with mock.patch("pathlib.Path.read_text", return_value=f'hub = "/h"\n[policy]\n{policy}'):
            return load_accounts(Path("/nowhere/accounts.toml"))[1]

    def test_default_minimum_and_refusal(self):
        self.assertEqual(self.load("").refresh_minutes, 5)
        self.assertEqual(self.load("refresh_minutes = 1").refresh_minutes, 1)
        for bad in ("refresh_minutes = 0.5", "refresh_minutes = 0", "refresh_minutes = 'x'"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.load(bad)


FAKE_PROBE = """#!/bin/sh
# Stands in for `fairshare probe <id>`: says how it was started, then sleeps.
{ echo "args $*"; echo "cwd $(pwd)"; echo "pid $$"; echo "pgid $(ps -o pgid= -p $$ | tr -d ' ')"
  [ -t 0 ] && echo "stdin tty" || echo "stdin $(wc -c < /dev/stdin | tr -d ' ')"; } \
  >> "$FAKE_LOG/probe-$$"
exec sleep "${FAKE_PROBE_SLEEP:-0}"
"""


class Fresh(Fleet):
    """The bar, the background probe and `fairshare probe`, on the fleet's clock."""

    def setUp(self):
        super().setUp()
        self.add("a")
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.probe = write(self.bin / "probe", FAKE_PROBE)
        self.probe.chmod(0o755)
        p = mock.patch.object(cli, "_fairshare_argv", lambda: [str(self.probe)])
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.stop_probes)

    def stop_probes(self):
        for p in self.log.glob("probe-*"):           # the fakes this test started, and only them
            try:
                os.kill(int(p.name.split("-")[1]), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    def capture(self, payload=None):
        payload = {"session_id": SID, "cwd": str(self.work), **(payload or {})}
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(
                json.dumps(payload).encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            return self.go("capture")

    def started(self, n=1, wait=3.0):
        """What each fake probe logged, once `n` have."""
        end = time.monotonic() + wait
        while time.monotonic() < end:
            logs = sorted(self.log.glob("probe-*"))
            if len(logs) >= n and all("stdin" in p.read_text() for p in logs):
                return [dict(l.split(" ", 1) for l in p.read_text().splitlines()) for p in logs]
            time.sleep(0.05)
        return [dict(l.split(" ", 1) for l in p.read_text().splitlines())
                for p in sorted(self.log.glob("probe-*"))]

    def limits(self, five, seven):
        return {"rate_limits": {"five_hour": {"used_percentage": five[0],
                                              "resets_at": NOW + five[1] * H},
                                "seven_day": {"used_percentage": seven[0],
                                              "resets_at": NOW + seven[1] * H}}}

    def test_an_equal_redraw_of_old_numbers_shows_as_of(self):
        self.snap("a", five=(10, 2), seven=(40, 50), at=NOW - 6 * 60)
        _, out, _ = self.capture(self.limits((10, 2), (40, 50)))
        self.assertEqual(out, "a · 5h 10% · 7d 40% · as of 07:54\n")
        self.assertEqual(store.load("a").captured_at, NOW - 6 * 60)       # not made fresh

    def test_fresh_numbers_show_no_time(self):
        self.snap("a", five=(10, 2), seven=(40, 50), at=NOW - 4 * 60)
        _, out, _ = self.capture(self.limits((10, 2), (40, 50)))
        self.assertEqual(out, "a · 5h 10% · 7d 40%\n")
        self.assertEqual(self.started(wait=0.3), [])                   # and no probe
        self.assertEqual(self.real_argv(), [sys.executable, "-P", "-m", "fairshare.cli"])

    def test_stale_usage_starts_one_detached_probe_in_the_sessions_folder(self):
        self.snap("a", five=(10, 2), seven=(40, 50), at=NOW - 6 * 60)
        with mock.patch.dict(os.environ, {"FAKE_PROBE_SLEEP": "5"}):
            t = time.monotonic()
            self.capture()
            took = time.monotonic() - t
        self.assertLess(took, 1.0)                                      # never waited for
        (seen,) = self.started()
        self.assertEqual(seen["args"], "probe a")
        self.assertEqual(Path(seen["cwd"]).resolve(), self.work.resolve())
        self.assertEqual(seen["stdin"], "0")                            # /dev/null
        self.assertEqual(seen["pgid"], seen["pid"])                     # its own session
        self.assertNotEqual(int(seen["pgid"]), os.getpgid(0))

    def test_never_two_at_once(self):
        with mock.patch.dict(os.environ, {"FAKE_PROBE_SLEEP": "5"}):
            self.capture()                                              # no snapshot: unknown
            self.clock[0] += 2 * claude.USAGE_TIMEOUT - 1
            self.capture()
            self.assertEqual(len(self.started(n=2, wait=0.5)), 1)
            self.clock[0] += 1
            self.capture()
            self.assertEqual(len(self.started(n=2)), 2)

    def test_the_probe_command_records_as_a_probe(self):
        self.snap("a", five=(10, 2), seven=(40, 50), at=NOW - H)
        self.serve_usage("a")
        self.assertEqual(self.go("probe", "a"), (0, "", ""))
        snap = store.load("a")
        self.assertEqual(snap.captured_at, NOW)
        self.assertEqual(snap.windows["seven_day"].used_pct, 96)

    def test_a_failed_probe_records_nothing_and_says_nothing(self):
        self.snap("a", five=(10, 2), seven=(40, 50), at=NOW - H)
        for argv in (("probe", "a"), ("probe", "nobody")):
            with self.subTest(argv=argv):
                self.assertEqual(self.go(*argv), (0, "", ""))
                self.assertEqual(store.load("a").captured_at, NOW - H)

    def test_capture_starts_the_real_probe_command(self):
        """The whole chain once, with this fairshare (not the fake): capture starts it, it asks
        the fake claude and records."""
        self.serve_usage("a")
        src = str(Path(cli.__file__).resolve().parent.parent)
        with mock.patch.object(cli, "_fairshare_argv", self.real_argv), \
                mock.patch.dict(os.environ, {"PYTHONPATH": src}):
            self.capture()
            end = time.monotonic() + 15
            while store.load("a") is None and time.monotonic() < end:
                time.sleep(0.1)
        self.assertEqual(store.load("a").windows["seven_day"].used_pct, 96)

    real_argv = staticmethod(cli._fairshare_argv)     # as imported, before any test patches it


TRANSCRIPT = FIXTURES / "claude-2.1.322" / "transcript.jsonl"
HIDDEN = ("BLOB", "THINKING", "TOOL RESULT", "SIDECHAIN")


class Stranded(Fleet):
    """Accounts a (whose session was stopped) and b; a session record on a in a work folder."""

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.add("a", orgId="org-a")
        self.add("b", orgId="org-b", extra='browser = "command:/usr/bin/true"\n')
        self.snap("a", five=(100, 3), seven=(50, 90))            # a is out for three hours
        self.snap("b", five=(10, 2), seven=(30, 90))
        self.probe_log = []
        p = mock.patch.object(cli, "_start_probe",
                              lambda acct, cwd, now: self.probe_log.append((acct.id, cwd)))
        p.start()
        self.addCleanup(p.stop)

    def policy(self, text):
        """A `[policy]` table in the config (after the last `add`)."""
        cfg = self.tmp / "accounts.toml"
        cfg.write_text(cfg.read_text().replace("\n\n[[account]]", f"\n[policy]\n{text}\n\n"
                                                                    "[[account]]", 1))

    def trust(self, account, folder=None):
        write(self.tmp / "hub/accounts" / account / ".claude.json", json.dumps(
            {"projects": {str((folder or self.work).resolve()): {"hasTrustDialogAccepted": True}}}))

    def record(self, sid=SID, account="a", stranded=True, cwd=None):
        store.note_session(SessionSeen(sid, str(cwd or self.work), str(TRANSCRIPT)), account,
                           NOW - 60, stranded=stranded)

    def started(self, *argv):
        """A process this test starts and owns (a stand-in launcher or bystander)."""
        p = subprocess.Popen(list(argv) or ["sleep", "30"])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p

    def hook(self, payload, env=None):
        raw = json.dumps({"session_id": SID, "transcript_path": str(TRANSCRIPT),
                          "cwd": str(self.work), "hook_event_name": "StopFailure",
                          "error": "rate_limit", **payload})
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(raw.encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a"),
                                             **(env or {})}):
            return self.go("hook", "stop-failure")


class StopFailureHook(Stranded):
    def test_records_stranded_starts_a_probe_and_says_nothing(self):
        self.assertEqual(self.hook({}), (0, "", ""))               # capture never saw it
        r = store.session(SID)
        self.assertEqual((r.account, r.stranded_at, r.cwd), ("a", NOW, str(self.work)))
        self.assertEqual(self.probe_log, [("a", str(self.work))])
        self.assertFalse((self.tmp / "state" / "requests").exists())

    def test_another_error_is_not_ours(self):
        self.hook({"error": "overloaded"})
        self.assertIsNone(store.session(SID))

    def test_signals_a_live_launcher_with_the_request(self):
        launcher = self.started()
        with mock.patch.object(cli, "_ancestors", return_value={launcher.pid}):
            self.assertEqual(self.hook({}, {"FAIRSHARE_LAUNCHER": str(launcher.pid),
                                            "CLAUDE_PID": "4321"}), (0, "", ""))
        self.assertEqual(launcher.wait(timeout=WAIT), -signal.SIGUSR1)   # the signal arrived
        self.assertEqual(store.take_request(launcher.pid), Request(SID, False, 4321))

    def test_ignores_a_dead_pid_and_a_stranger(self):
        dead = self.started("true")
        dead.wait(timeout=WAIT)
        stranger = self.started()
        for pid in (dead.pid, stranger.pid, 1, "x"):
            with self.subTest(pid=pid):
                self.assertEqual(self.hook({}, {"FAIRSHARE_LAUNCHER": str(pid)}), (0, "", ""))
                self.assertIsNone(store.take_request(dead.pid))
                self.assertIsNone(store.take_request(stranger.pid))
        self.assertIsNone(stranger.poll())                            # never signalled
        self.assertIsNotNone(store.session(SID).stranded_at)

    def test_the_whole_chain_from_a_launcher_that_is_an_ancestor(self):
        """For real: a stand-in launcher runs the hook as its child; the hook finds it among its
        ancestors and signals it."""
        launcher = (
            "import os, signal, subprocess, sys, time\n"
            "got = []\n"
            "signal.signal(signal.SIGUSR1, lambda *_: got.append(1))\n"
            "env = dict(os.environ, FAIRSHARE_LAUNCHER=str(os.getpid()))\n"
            "subprocess.run([sys.executable, '-P', '-m', 'fairshare.cli', 'hook', 'stop-failure'],"
            " input=sys.argv[1].encode(), env=env, timeout=20)\n"
            "end = time.time() + 5\n"
            "while not got and time.time() < end: time.sleep(0.05)\n"
            "print('signalled' if got else 'not signalled', os.getpid())\n")
        raw = json.dumps({"session_id": SID, "cwd": str(self.work), "error": "rate_limit"})
        r = subprocess.run([sys.executable, "-c", launcher, raw], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "PYTHONPATH": SRC,
                                            "CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")})
        said, pid = r.stdout.split()
        self.assertEqual(said, "signalled", r.stderr)
        self.assertEqual(store.take_request(int(pid)), Request(SID, False))


class BarMark(Stranded):
    def capture(self, *argv):
        payload = {"session_id": SID, "cwd": str(self.work)}
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(
                json.dumps(payload).encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "hub/accounts/a")}):
            return self.go("capture", *argv)

    def test_set_while_the_account_is_out(self):
        self.record()
        _, out, _ = self.capture()
        self.assertTrue(out.endswith(" · \033[31mout · ! fairshare pickup\033[0m\n"), out)

    def test_set_inside_the_grace_hour_too(self):
        """A spent 5h window is out until it resets, however soon (the grace hour is for a nearly
        full one only)."""
        self.record()
        self.snap("a", five=(100, 0.5), seven=(50, 90), at=NOW)
        self.assertIn("out · ! fairshare pickup", self.capture()[1])
        self.assertFalse(cli._can_work(cli._survey(), "a"))

    def test_after_the_users_own_line_on_a_line_of_its_own(self):
        self.record()
        code, out, _ = self.capture("--then", "true")
        self.assertEqual((code, out), (0, "\033[31mout · ! fairshare pickup\033[0m\n"))

    def test_not_set_for_a_session_that_was_not_stopped(self):
        self.record(stranded=None)
        self.assertNotIn("pickup", self.capture()[1])

    def test_cleared_once_newer_numbers_say_it_can_work(self):
        self.record()
        self.snap("a", five=(5, 4), seven=(50, 90), at=NOW)          # the window reset
        self.assertNotIn("pickup", self.capture()[1])
        self.assertIsNone(store.session(SID).stranded_at)
        self.snap("a", five=(100, 3), seven=(50, 90), at=NOW)         # and the mark stays gone
        self.assertNotIn("pickup", self.capture()[1])


SUPERVISOR = (
    "import json, os, sys; sys.path.insert(0, sys.argv[1]); "
    "from fairshare import session; "
    "session.STOP_WAIT = float(os.environ.get('STOP_WAIT', '10')); "
    "answers = json.loads(os.environ['ANSWERS']); calls = []\n"
    "def asked(pid):\n"
    "    calls.append(pid); open(os.path.join(os.environ['D'], 'asked-%d' % len(calls)), 'w')\n"
    "    return answers[len(calls) - 1] if len(calls) <= len(answers) else False\n"
    "code = session.run_child(json.loads(sys.argv[2]), dict(os.environ), asked)\n"
    "print(json.dumps({'code': code, 'calls': len(calls)}))")


class LauncherWait(Supervised):
    """run_child with a pickup callback, run for real in a supervisor this test starts."""

    def start(self, mode, answers, stop_wait=None):
        cmd = [sys.executable, str(self.dir / "child.py"), str(self.dir), mode]
        env = {**os.environ, "ANSWERS": json.dumps(answers), "D": str(self.dir),
               **({"STOP_WAIT": str(stop_wait)} if stop_wait is not None else {})}
        self.parent = subprocess.Popen([sys.executable, "-c", SUPERVISOR, SRC, json.dumps(cmd)],
                                       env=env, start_new_session=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.reap)
        until("the child", lambda: (self.dir / "pid").exists())
        return self.parent

    def ask(self, n=1):
        os.kill(self.parent.pid, signal.SIGUSR1)
        until("the request", lambda: (self.dir / f"asked-{n}").exists())

    def result(self):
        return json.loads(self.parent.communicate(timeout=WAIT)[0])

    def test_an_unconfirmed_request_goes_back_to_waiting(self):
        self.start("hold", [False])
        self.ask()
        time.sleep(0.2)
        self.assertFalse((self.dir / "got-TERM").exists())          # the child runs on
        (self.dir / "stop").touch()
        self.assertEqual(self.result(), {"code": 5, "calls": 1})

    def test_a_confirmed_one_stops_the_child(self):
        self.start("hold", [False, True])
        self.ask(1)
        self.ask(2)
        self.assertEqual(self.result(), {"code": session.PICKUP, "calls": 2})
        self.assertTrue((self.dir / "got-TERM").exists())

    def test_a_child_that_ignores_sigterm_gets_sigkill_after_the_wait(self):
        (self.dir / "child.py").write_text(
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: open(os.path.join(sys.argv[1], 'got-TERM'),"
            " 'w'))\n"
            "open(os.path.join(sys.argv[1], 'pid'), 'w').write(str(os.getpid()))\n"
            "while True: time.sleep(0.05)\n")
        self.start("stubborn", [True], stop_wait=0.5)
        child = int((self.dir / "pid").read_text())
        t = time.monotonic()
        self.ask()
        self.assertEqual(self.result(), {"code": session.PICKUP, "calls": 1})
        self.assertGreaterEqual(time.monotonic() - t, 0.4)
        self.assertTrue((self.dir / "got-TERM").exists())
        with self.assertRaises(ProcessLookupError):                  # reaped: gone
            os.kill(child, 0)


class Countdown(unittest.TestCase):
    """The countdown on a real pseudo-terminal."""

    def setUp(self):
        self.master, slave = pty.openpty()
        self.tty = os.fdopen(slave, "r")
        self.addCleanup(self.tty.close)
        self.addCleanup(os.close, self.master)
        attrs = termios.tcgetattr(slave)      # no echo: nobody reads this terminal's screen, and a
        attrs[3] &= ~termios.ECHO             # mode change would wait for the echo to drain
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self.before = termios.tcgetattr(slave)

    def run_it(self, keys=b"", seconds=3):
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", self.tty), mock.patch.object(sys, "stderr", err):
            if keys:
                orig = cli.termios.tcflush

                def flushed(fd, how):                 # type the keys after the old input went
                    orig(fd, how)
                    os.write(self.master, keys)
                with mock.patch.object(cli.termios, "tcflush", flushed):
                    got = cli._countdown(lambda n: f"in {n} s", seconds)
            else:
                got = cli._countdown(lambda n: f"in {n} s", seconds)
        self.assertEqual(self.attrs(termios.tcgetattr(self.tty.fileno())),
                         self.attrs(self.before))                    # restored
        return got, err.getvalue()

    @staticmethod
    def attrs(a):
        """The settings, without PENDIN: the kernel's own note that input is pending (macOS
        sets it after a flush), not a setting anyone made."""
        return [a[0], a[1], a[2], a[3] & ~getattr(termios, "PENDIN", 0), *a[4:]]

    def test_enter_goes_on_now(self):
        t = time.monotonic()
        self.assertTrue(self.run_it(b"\r")[0])
        self.assertLess(time.monotonic() - t, 1)

    def test_c_cancels_and_other_keys_are_ignored(self):
        self.assertFalse(self.run_it(b"xc")[0])

    def test_the_time_running_out_goes_on(self):
        got, err = self.run_it(seconds=2)
        self.assertTrue(got)
        self.assertEqual(err, "\rin 2 s\rin 1 s\n")

    def test_typing_before_it_started_is_dropped(self):
        os.write(self.master, b"c")                                    # typed into the old session
        self.assertTrue(self.run_it(seconds=1)[0])


class PickupLaunch(Stranded):
    """The launcher: a fake child that asks for pickups, through the real launch path."""

    def launches(self, *argv, requests=(), tty=False, answers=(), countdown=True):
        """Run the CLI with run_child faked: the n-th launch, when `requests[n]` is given, files
        that request (session, manual, child pid) and asks, as SIGUSR1 would, then returns what
        the real one would."""
        self.calls = []

        def run_child(cmd, env, on_request=None):
            self.calls.append((cmd, env, os.getcwd()))
            n = len(self.calls) - 1
            if n < len(requests) and requests[n]:
                before, req = requests[n]
                before()
                if req:
                    store.save_request(os.getpid(), Request(*req, 4321))
                return session.PICKUP if on_request(4321) else 0
            return 0
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(session, "run_child", run_child), \
                mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch.object(cli, "_countdown", return_value=countdown) as counted, \
                mock.patch("builtins.input", side_effect=list(answers)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(list(argv))
        self.counted = counted
        return code, err.getvalue()

    def accounts_of(self):
        return [Path(env["CLAUDE_CONFIG_DIR"]).name for _, env, _ in self.calls]

    def test_a_confirmed_request_resumes_the_same_session_on_the_best_account(self):
        self.policy("resume_wait_minutes = 30")                     # opted in; a is out for 3h
        self.record()
        code, err = self.launches("run", "-a", "a", requests=[(lambda: None, (SID, False))])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.accounts_of(), ["a", "b"])
        cmd, env, cwd = self.calls[1]
        self.assertEqual(cmd[1:], ["--resume", SID, "fairshare moved this session from account a "
                                   "to account b because a hit its usage limit. Continue where "
                                   "you left off."])
        rec = store.session(SID)          # the same record; its first capture writes the account
        self.assertEqual((rec.account, rec.stranded_at, rec.cwd), ("a", None, str(self.work)))
        self.assertEqual([r.id for r in store.sessions()], [SID])
        self.assertEqual(Path(cwd).resolve(), self.work.resolve())
        self.assertEqual(env["FAIRSHARE_LAUNCHER"], str(os.getpid()))
        self.assertEqual(env["FAIRSHARE_CONTINUES"], f"{os.getpid()}:{SID}")
        self.counted.assert_not_called()                             # no terminal: at once
        self.assertIsNone(store.take_request(os.getpid()))           # consumed

    def test_an_unconfirmed_request_changes_nothing_and_says_nothing(self):
        self.record()
        self.snap("a", five=(10, 3), seven=(50, 90))                # a can still work: a 429 passed
        self.launches("run", "-a", "a")                              # links made: said once
        _, before = self.launches("run", "-a", "a")
        code, err = self.launches("run", "-a", "a", requests=[(lambda: None, (SID, False))])
        self.assertEqual((code, self.accounts_of(), err), (0, ["a"], before))

    def test_a_request_for_another_process_or_an_unstranded_session_is_ignored(self):
        self.record(stranded=None)
        self.launches("run", "-a", "a", requests=[(lambda: None, (SID, False))])
        self.assertEqual(self.accounts_of(), ["a"])
        self.record()
        self.launches("run", "-a", "a", requests=[(lambda: store.save_request(
            os.getpid(), Request(SID, False, 99)), None)])                    # pid 99: not our child
        self.assertEqual(self.accounts_of(), ["a"])

    def test_a_manual_request_skips_the_check_and_says_so(self):
        self.record(stranded=None)
        self.snap("a", five=(10, 3), seven=(50, 90))
        code, err = self.launches("run", "-a", "a", requests=[(lambda: None, (SID, True))])
        self.assertEqual(self.accounts_of(), ["a", "b"])            # b ranks first
        self.assertIn("note: a can still take work; the session is picked up as asked", err)
        self.assertEqual(self.calls[1][0][1:], ["--resume", SID, "fairshare moved this session "
                                                "from account a to account b because a move was "
                                                "asked for. Continue where you left off."])

    def test_a_second_pickup_works_in_the_same_launcher(self):
        self.add("c", orgId="org-c")
        self.snap("c", five=(10, 2), seven=(60, 90))
        self.policy("resume_wait_minutes = 30")
        self.record()
        self.record(sid="second", account="b")

        def b_runs_out():
            self.snap("b", five=(100, 2), seven=(30, 90))
        code, _ = self.launches("run", "-a", "a", requests=[
            (lambda: None, (SID, False)), (b_runs_out, ("second", False))])
        self.assertEqual(self.accounts_of(), ["a", "b", "c"])
        self.assertEqual(self.calls[2][0][1:3], ["--resume", "second"])
        self.assertIn("from account b to account c because b hit", self.calls[2][0][3])

    def test_with_none_eligible_a_manual_request_takes_the_refusal_flow(self):
        self.record()
        for x in "ab":
            self.trust(x)                                            # on a terminal: not asked
        self.snap("b", five=(100, 2), seven=(30, 90))
        code, err = self.launches("run", "-a", "a", requests=[(lambda: None, (SID, True))],
                                  tty=True, answers=["s"])
        self.assertIn("no account is eligible", err)
        self.assertEqual(self.accounts_of(), ["a", "b"])            # started anyway, on the top
        self.assertEqual(self.calls[1][0][1:3], ["--resume", SID])

    def test_the_countdown_names_the_limit_and_cancel_says_how_to_resume(self):
        self.record()
        texts, err = [], io.StringIO()
        rec, s = store.session(SID), cli._survey()
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_countdown", lambda text, *a: texts.append(text(10))
                                  or False), mock.patch("sys.stderr", err), \
                mock.patch.object(cli, "_route") as routed:
            self.assertEqual(cli._continue(rec, s, Request(SID, False)), 0)
        routed.assert_not_called()
        self.assertEqual(texts, ["a hit its limit (5h resets 11:00). Continuing on b in 10 s · "
                                 "Enter = now · c = cancel"])
        self.assertIn(f"fairshare run -a a -- --resume {SID}", err.getvalue())


class PickupCommand(Stranded):
    """`fairshare pickup`: in a session, from a terminal, in the background."""

    def setUp(self):
        super().setUp()
        real = self.bin / "claude-real"
        os.rename(self.bin / "claude", real)
        write(self.bin / "claude", f'#!/bin/sh\nname=$(basename "$CLAUDE_CONFIG_DIR")\n'
                                   f'[ "$1" = "agents" ] || pwd -P > "$FAKE_LOG/cwd-$name"\n'
                                   f'[ "$1" = "--bg" ] && printf "%s\\n" "$@" > '
                                   f'"$FAKE_LOG/bg-$name"\n'
                                   f'exec "{real}" "$@"\n').chmod(0o755)


    def pickup(self, *argv, env=None, tty=False):
        """`fairshare pickup ...`; a foreground launch is recorded (self.fg: its command and the
        folder it started in), not made."""
        with mock.patch.dict(os.environ, env or {}):
            code, out, err = self.go("pickup", *argv, tty=tty)
        self.fg = [(c.args[0], os.getcwd()) for c in self.launched.call_args_list]
        return code, out, err

    def bg_args(self, account):
        """The arguments of the account's last background start (a later `agents` listing
        logs its own)."""
        path = self.log / f"bg-{account}"
        return path.read_text().splitlines() if path.exists() else []

    def test_inside_a_session_with_a_launcher_it_asks_the_launcher(self):
        self.record()
        launcher = self.started()
        with mock.patch.object(cli, "_ancestors", return_value={launcher.pid}):
            code, out, _ = self.pickup(env={"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_PID": "777",
                                            "FAIRSHARE_LAUNCHER": str(launcher.pid)})
        self.assertEqual((code, out), (0, "the launcher will stop this session and continue on "
                                          "the best account\n"))
        self.assertEqual(launcher.wait(timeout=WAIT), -signal.SIGUSR1)
        self.assertEqual(store.take_request(launcher.pid), Request(SID, True, 777))

    def test_inside_a_session_without_one_it_starts_in_the_background(self):
        self.record()
        self.trust("b")
        code, _, err = self.pickup(env={"CLAUDE_CODE_SESSION_ID": SID})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bg_args("b"), ["--bg", "--name", "pickup-0a1b2c3d", "--resume",
                                             SID, "fairshare moved this session from account a "
                                             "to account b because a hit its usage limit. "
                                             "Continue where you left off."])
        self.assertEqual(store.session(SID).account, "b")           # stranded: in place
        self.assertEqual(Path((self.log / "cwd-b").read_text().strip()), self.work.resolve())
        env = dict(l.split("=", 1) for l in (self.log / "env-b").read_text().splitlines()
                   if "=" in l)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.tmp / "hub/accounts/b"))
        for gone in ("CLAUDE_CODE_SESSION_ID", "FAIRSHARE_LAUNCHER", "BROWSER"):
            self.assertNotIn(gone, env)
        self.assertEqual(self.fg, [])

    def test_background_copies_trust_to_the_top_account_first(self):
        self.record()
        self.snap("a", five=(10, 3), seven=(10, 20))                 # a ranks first ...
        self.trust("b")                                              # ... but only b trusts
        code, _, err = self.pickup(SID[:6], "--bg")
        self.assertEqual(code, 0, err)
        self.assertIn("note: a can still take work", err)
        self.assertIn(f"a now trusts {self.work.resolve()} (copied from b)", err)
        self.assertEqual(self.bg_args("a")[0], "--bg")
        self.assertTrue(claude.trusts(self.tmp / "hub/accounts/a", self.work.resolve()))

    def test_background_without_copy_takes_only_an_account_that_trusts_the_folder(self):
        self.record()
        self.snap("a", five=(10, 3), seven=(10, 20))                 # a ranks first ...
        self.trust("b")                                              # ... but only b trusts
        store.remember_trust_only(self.work.resolve())               # and copying is refused
        code, _, err = self.pickup(SID[:6], "--bg")
        self.assertEqual(code, 0, err)
        self.assertNotIn("now trusts", err)
        self.assertEqual(self.bg_args("b")[0], "--bg")
        self.assertNotIn("--bg", self.bg_args("a"))                 # a was only asked who it is

    def test_none_eligible_and_trusting(self):
        self.record()
        code, _, err = self.pickup(SID, "--bg")
        self.assertEqual(code, 1)
        self.assertIn("eligible: b, none of which trusts", err)
        self.snap("b", five=(100, 2), seven=(30, 90))
        self.assertIn("no account can take work now; the soonest is b, usable at 10:00; nothing "
                      "done", self.pickup(SID, "--bg")[2])

    def test_session_by_prefix(self):
        self.record()
        self.record(sid="0a1b2c3d-9999")
        self.record(sid="ffff")
        for x in "ab":
            self.trust(x)
        code, _, err = self.pickup("0a1b", tty=True)
        self.assertEqual(code, 2)
        self.assertIn("'0a1b' matches 2 sessions", err)
        self.assertIn(SID, err)
        self.assertEqual(self.pickup("zzz", tty=True)[0], 2)
        code, _, err = self.pickup("fff", tty=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fg[0][0][1:3], ["--resume", "ffff"])

    def test_foreground_from_a_terminal_runs_in_the_recorded_folder(self):
        self.record()
        for x in "ab":
            self.trust(x)
        code, _, err = self.pickup(SID[:8], tty=True)
        self.assertEqual(code, 0, err)
        (cmd, cwd), = self.fg
        self.assertEqual(Path(cwd).resolve(), self.work.resolve())
        self.assertEqual(cmd[0], str(self.bin / "claude"))
        self.assertEqual(cmd[1:3], ["--resume", SID])

    def test_a_gone_folder_is_refused(self):
        self.record(cwd=self.tmp / "gone")
        for argv in ((SID,), (SID, "--bg")):
            with self.subTest(argv=argv):
                code, _, err = self.pickup(*argv, tty=True)
                self.assertEqual(code, 1)
                self.assertIn(f"{self.tmp / 'gone'} is gone", err)
        self.assertEqual(self.fg, [])

    def test_outside_a_session_with_no_id(self):
        self.assertEqual(self.pickup()[0], 2)


class Transcript(Stranded):
    def test_the_fixture_prints_the_four_visible_messages_only(self):
        self.record()
        code, out, _ = self.go("transcript", SID[:4])
        self.assertEqual(code, 0)
        self.assertEqual(out, "user: Fix the parser so dates without a year work.\n\n"
                              "assistant: I'll look at the parser first.\n\n"
                              "assistant: Fixed: a missing year now takes the next date after now."
                              " Tests pass.\n\n"
                              "user: Now add a test for December to January.\n")
        for hidden in HIDDEN:
            self.assertNotIn(hidden, out)

    def test_tail(self):
        self.record()
        out = self.go("transcript", SID, "--tail", "2")[1]
        self.assertEqual(out.split("\n\n")[0], "assistant: Fixed: a missing year now takes the "
                                               "next date after now. Tests pass.")
        with self.assertRaises(SystemExit):
            self.go("transcript", SID, "--tail", "0")

    def test_an_unreadable_or_empty_file(self):
        for name, data in (("missing.jsonl", None), ("blob.jsonl", b"\xff\xfe"),
                           ("empty.jsonl", b'{"type": "cost-state"}\n')):
            with self.subTest(name=name):
                path = self.tmp / name
                if data is not None:
                    path.write_bytes(data)
                store.note_session(SessionSeen("s-" + name, str(self.work), str(path)), "a", NOW)
                code, out, err = self.go("transcript", "s-" + name)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"{path}: could not be read, or its layout was not understood", err)

    def test_a_record_with_no_transcript_says_so(self):
        store.note_session(SessionSeen("s-none", str(self.work), None), "a", NOW)
        self.assertEqual(self.go("transcript", "s-none"),
                         (1, "", "fairshare: no transcript recorded for s-none\n"))


if __name__ == "__main__":
    unittest.main()
