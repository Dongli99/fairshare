"""session.run_child, run for real: a supervisor process (in its own session, so a signal sent
to its process group stands in for the terminal's) runs a fake child. Every wait has a timeout,
and the whole group is killed afterwards, so a failure cannot hang the suite. No claude is
involved: the child is this Python running a small script."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import fairshare

SRC = str(Path(fairshare.__file__).resolve().parent.parent)
SUPERVISOR = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); "
              "from fairshare.session import run_child; "
              "sys.exit(run_child(json.loads(sys.argv[2]), dict(os.environ)))")
CHILD = """
import os, signal, sys, time
d, mode = sys.argv[1], sys.argv[2]
def note(name): open(os.path.join(d, 'got-' + name), 'w').close()
if mode == 'exit':
    sys.exit(int(sys.argv[3]))
if mode == 'die':
    os.kill(os.getpid(), getattr(signal, 'SIG' + sys.argv[3]))
    time.sleep(10)
if mode == 'hold':       # INT and QUIT: note and carry on. TERM and HUP: note and exit 4.
    for n in ('INT', 'QUIT'):
        signal.signal(getattr(signal, 'SIG' + n), lambda s, f, n=n: note(n))
    for n in ('TERM', 'HUP'):
        signal.signal(getattr(signal, 'SIG' + n), lambda s, f, n=n: (note(n), os._exit(4)))
open(os.path.join(d, 'pid'), 'w').write(str(os.getpid()))
while not os.path.exists(os.path.join(d, 'stop')):
    time.sleep(0.02)
sys.exit(5)
"""
WAIT = 10.0


def until(what, cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def state(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                       timeout=WAIT)
    return r.stdout.strip()


class Supervised(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.dir = Path(t.name)
        (self.dir / "child.py").write_text(CHILD)
        self.parent = None

    def start(self, mode, *extra, env=None):
        cmd = [sys.executable, str(self.dir / "child.py"), str(self.dir), mode, *extra]
        self.parent = subprocess.Popen(
            [sys.executable, "-c", SUPERVISOR, SRC, json.dumps(cmd)],
            env={**os.environ, **(env or {})}, start_new_session=True, stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap)
        return self.parent

    def start_in_shell(self, mode, *extra):
        """The supervisor as a shell runs a job: in a process group of its own, in the session of
        a stand-in shell that stays alive, so the group is not orphaned and a stop holds. Returns
        the supervisor's pid (its group's id); the shell exits with the supervisor's code."""
        cmd = [sys.executable, str(self.dir / "child.py"), str(self.dir), mode, *extra]
        job = [sys.executable, "-c", SUPERVISOR, SRC, json.dumps(cmd)]
        shell = ("import os, subprocess, sys; "
                 "p = subprocess.Popen(sys.argv[2:], process_group=0, stderr=subprocess.DEVNULL); "
                 "open(sys.argv[1], 'w').write(str(p.pid)); sys.exit(p.wait())")
        self.shell = subprocess.Popen([sys.executable, "-c", shell, str(self.dir / "job"), *job],
                                      start_new_session=True)
        until("the job to start", lambda: (self.dir / "job").exists()
              and (self.dir / "job").read_text())
        job_pid = int((self.dir / "job").read_text())
        def reap():
            for g in (job_pid, self.shell.pid):
                try:
                    os.killpg(g, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self.shell.wait(timeout=WAIT)
        self.addCleanup(reap)
        return job_pid

    def reap(self):
        try:
            os.killpg(self.parent.pid, signal.SIGKILL)       # the group: parent and child
        except (ProcessLookupError, PermissionError):
            pass
        self.parent.wait(timeout=WAIT)

    def code(self):
        return self.parent.wait(timeout=WAIT)

    def ready(self):
        """Wait for the child to be running; return its pid."""
        until("the child to start", lambda: (self.dir / "pid").exists()
              and (self.dir / "pid").read_text())
        return int((self.dir / "pid").read_text())

    def stop_child(self):
        (self.dir / "stop").touch()


class ExitCode(Supervised):
    def test_the_childs_code_passes_through(self):
        for n in (0, 1, 7, 130):
            with self.subTest(code=n):
                self.start("exit", str(n))
                self.assertEqual(self.code(), n)

    def test_death_by_signal_is_128_plus_the_number(self):
        # USR2, not SEGV: a self-inflicted SEGV makes macOS show a "Python quit unexpectedly" alert.
        for name, n in (("TERM", 15), ("KILL", 9), ("USR2", int(signal.SIGUSR2))):
            with self.subTest(signal=name):
                self.start("die", name)
                self.assertEqual(self.code(), 128 + n)

    def test_a_command_that_cannot_start_is_126_or_127_with_a_message(self):
        for cmd, code in (([str(self.dir / "missing")], 127), ([str(self.dir)], 126)):
            with self.subTest(cmd=cmd):
                p = subprocess.run([sys.executable, "-c", SUPERVISOR, SRC, json.dumps(cmd)],
                                   capture_output=True, text=True, timeout=WAIT)
                self.assertEqual(p.returncode, code)
                self.assertIn("cannot start", p.stderr)

    def test_the_child_gets_exactly_the_given_environment(self):
        script = self.dir / "env.py"
        script.write_text("import os, sys; sys.exit(0 if os.environ.get('FS_ONLY') == '1' "
                          "and 'HOME' not in os.environ else 9)")
        p = subprocess.run([sys.executable, "-c",
                            "import json, sys; sys.path.insert(0, sys.argv[1]); "
                            "from fairshare.session import run_child; "
                            "sys.exit(run_child(json.loads(sys.argv[2]), {'FS_ONLY': '1'}))",
                            SRC, json.dumps([sys.executable, str(script)])], timeout=WAIT)
        self.assertEqual(p.returncode, 0)


class Terminal(Supervised):
    def test_the_parent_survives_sigint_and_sigquit_while_the_child_handles_them(self):
        p = self.start("hold")
        self.ready()
        for name in ("INT", "QUIT"):
            os.killpg(p.pid, getattr(signal, "SIG" + name))      # what the terminal does
            until(f"the child to see {name}", lambda: (self.dir / f"got-{name}").exists())
        time.sleep(0.2)
        self.assertIsNone(p.poll())                               # the parent is still there
        self.stop_child()
        self.assertEqual(self.code(), 5)                          # and passes the code on

    def test_ctrl_z_then_fg_stops_the_whole_job_and_resumes_it(self):
        p = self.start_in_shell("hold")
        pid = self.ready()
        # Ctrl-Z: the terminal sends SIGTSTP to the job's group. The parent does nothing with
        # it and the child stops...
        os.killpg(p, signal.SIGTSTP)
        until("the child to stop", lambda: state(pid).startswith("T"))
        # ...and the parent stops itself, so the shell would get its prompt back.
        until("the parent to stop", lambda: state(p).startswith("T"))
        # fg: the shell continues the group; the parent continues the child too.
        os.killpg(p, signal.SIGCONT)
        until("the child to run again", lambda: state(pid) and not state(pid).startswith("T"))
        until("the parent to run again", lambda: not state(p).startswith("T"))
        self.stop_child()
        self.assertEqual(self.shell.wait(timeout=WAIT), 5)        # the code still passes through

    def test_with_no_shell_left_a_stopped_child_is_continued_and_the_parent_never_stops(self):
        # This harness's group is orphaned (no parent in its session): nothing could continue a
        # stopped parent, as after its terminal closed.
        p = self.start("hold")
        pid = self.ready()
        os.kill(pid, signal.SIGSTOP)
        until("the child to run again", lambda: (self.dir / "pid").exists()
              and state(pid) and not state(pid).startswith("T"))
        self.assertFalse(state(p.pid).startswith("T"))
        self.assertEqual(os.waitpid(p.pid, os.WNOHANG | os.WUNTRACED), (0, 0))
        self.stop_child()
        self.assertEqual(self.code(), 5)

    def test_sigterm_and_sighup_to_the_parent_reach_the_child(self):
        for name in ("TERM", "HUP"):
            with self.subTest(signal=name):
                for f in self.dir.glob("*"):
                    if f.name != "child.py":
                        f.unlink()
                p = self.start("hold")
                self.ready()
                os.kill(p.pid, getattr(signal, "SIG" + name))     # to the parent alone
                self.assertEqual(self.code(), 4)                  # the child's own exit
                self.assertTrue((self.dir / f"got-{name}").exists())

    def test_the_childs_own_signal_dispositions_are_default(self):
        """The parent's no-op handlers must not leak into the child as SIG_IGN, which survives
        exec: the child would then ignore Ctrl-C and Ctrl-Z."""
        script = self.dir / "disp.py"
        script.write_text(
            "import signal, sys\n"
            "names = ('SIGINT', 'SIGQUIT', 'SIGTSTP', 'SIGTERM', 'SIGHUP')\n"
            "sys.exit(9 if any(signal.getsignal(getattr(signal, n)) == signal.SIG_IGN\n"
            "                  for n in names) else 0)\n")
        p = subprocess.run([sys.executable, "-c", SUPERVISOR, SRC,
                            json.dumps([sys.executable, str(script)])], timeout=WAIT)
        self.assertEqual(p.returncode, 0)

if __name__ == "__main__":
    unittest.main()
