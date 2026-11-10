"""Run one interactive child session and stay out of its way. Provider-neutral: a launch, and
later a handoff that stops the child with SIGTERM and starts the next account, both use it."""
from __future__ import annotations

import os
import signal
import subprocess
import sys


def run_child(cmd: list[str], env: dict[str, str]) -> int:
    """Start `cmd` as a child and wait for it; return its exit code (128 + n when signal n killed
    it). The terminal sends Ctrl-C, Ctrl-\\ and Ctrl-Z to the whole foreground process group, so
    the child already receives them; this parent must only survive and keep waiting:
    - SIGINT, SIGQUIT, SIGTSTP do nothing here (handlers, not SIG_IGN, so the child still starts
      with the default action for each);
    - when the child stops, this process stops too (SIGSTOP), so the shell gets its prompt back;
      when it is continued (`fg`), the child is continued and the wait resumes;
    - SIGTERM and SIGHUP are forwarded to the child."""
    child: subprocess.Popen | None = None

    def forward(signum, _frame):
        if child is not None and child.returncode is None:
            try:
                os.kill(child.pid, signum)
            except ProcessLookupError:
                pass

    handlers = {signal.SIGINT: lambda *_: None, signal.SIGQUIT: lambda *_: None,
                signal.SIGTSTP: lambda *_: None,
                signal.SIGTERM: forward, signal.SIGHUP: forward}
    saved = {s: signal.signal(s, h) for s, h in handlers.items()}
    try:
        try:
            child = subprocess.Popen(cmd, env=env)
        except OSError as e:
            print(f"fairshare: cannot start {cmd[0]}: {e.strerror}", file=sys.stderr)
            return 127 if isinstance(e, FileNotFoundError) else 126
        while True:
            _, status = os.waitpid(child.pid, os.WUNTRACED)
            if os.WIFSTOPPED(status):
                os.kill(os.getpid(), signal.SIGSTOP)     # returns once something continues us
                os.kill(child.pid, signal.SIGCONT)
                continue
            child.returncode = (os.WEXITSTATUS(status) if os.WIFEXITED(status)
                                else 128 + os.WTERMSIG(status))
            return child.returncode
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
