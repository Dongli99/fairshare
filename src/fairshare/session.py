"""Run one interactive child session and stay out of its way. Provider-neutral: a launch, and a
pickup that stops the child with SIGTERM and starts the next account, both use it."""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from typing import Callable

PICKUP = -1          # run_child's answer when it stopped the child for a pickup (not an exit code)
STOP_WAIT = 10.0     # seconds a child stopped for a pickup gets to exit on SIGTERM; then SIGKILL


def run_child(cmd: list[str], env: dict[str, str],
              on_request: Callable[[int], bool] | None = None) -> int:
    """Start `cmd` as a child and wait for it; return its exit code (128 + n when signal n killed
    it). The terminal sends Ctrl-C, Ctrl-\\ and Ctrl-Z to the whole foreground process group, so
    the child already receives them; this parent must only survive and keep waiting:
    - SIGINT, SIGQUIT, SIGTSTP do nothing here (handlers, not SIG_IGN, so the child still starts
      with the default action for each);
    - when the child stops, this process stops too (SIGSTOP), so the shell gets its prompt back;
      when it is continued (`fg`), the child is continued and the wait resumes;
    - SIGTERM and SIGHUP are forwarded to the child.
    With `on_request`, SIGUSR1 asks for a pickup: `on_request(child pid)` is called in the wait
    (not in the signal handler); False goes back to waiting, True stops the child (SIGTERM, then
    SIGKILL after STOP_WAIT seconds) and returns PICKUP. Without it SIGUSR1 keeps its default.
    The wait sleeps on a pipe that every signal wakes (`signal.set_wakeup_fd`), so a request
    arriving at any moment is seen and an exit status is never lost."""
    child: subprocess.Popen | None = None
    asked = False

    def forward(signum, _frame):
        if child is not None and child.returncode is None:
            try:
                os.kill(child.pid, signum)
            except ProcessLookupError:
                pass

    def request(_signum, _frame):
        nonlocal asked
        asked = True

    handlers = {signal.SIGINT: lambda *_: None, signal.SIGQUIT: lambda *_: None,
                signal.SIGTSTP: lambda *_: None, signal.SIGCHLD: lambda *_: None,
                signal.SIGTERM: forward, signal.SIGHUP: forward,
                **({signal.SIGUSR1: request} if on_request else {})}
    wake_r, wake_w = os.pipe()
    for fd in (wake_r, wake_w):
        os.set_blocking(fd, False)
    saved = {s: signal.signal(s, h) for s, h in handlers.items()}
    saved_fd = signal.set_wakeup_fd(wake_w)
    try:
        try:
            child = subprocess.Popen(cmd, env=env)
        except OSError as e:
            print(f"fairshare: cannot start {cmd[0]}: {e.strerror}", file=sys.stderr)
            return 127 if isinstance(e, FileNotFoundError) else 126
        while True:
            pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if pid == 0:
                if asked:
                    asked = False
                    if on_request(child.pid):
                        _stop(child.pid)
                        child.returncode = PICKUP
                        return PICKUP
                    continue
                select.select([wake_r], [], [])          # until a signal (SIGCHLD included)
                _drain(wake_r)
                continue
            if os.WIFSTOPPED(status):
                os.kill(os.getpid(), signal.SIGSTOP)     # returns once something continues us
                os.kill(child.pid, signal.SIGCONT)
                continue
            child.returncode = (os.WEXITSTATUS(status) if os.WIFEXITED(status)
                                else 128 + os.WTERMSIG(status))
            return child.returncode
    finally:
        signal.set_wakeup_fd(saved_fd)
        for s, h in saved.items():
            signal.signal(s, h)
        os.close(wake_r)
        os.close(wake_w)


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 512):
            pass
    except BlockingIOError:
        pass


def _stop(pid: int) -> None:
    """SIGTERM the child and reap it; SIGKILL when it has not exited after STOP_WAIT seconds."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    end = time.monotonic() + STOP_WAIT
    while time.monotonic() < end:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done and not os.WIFSTOPPED(status):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
