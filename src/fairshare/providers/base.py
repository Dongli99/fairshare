"""What a provider adapter must do. Add a provider = one new file implementing this.
Every fact about the provider that its vendor may change (binary name, arguments, environment
variables, file and folder names, JSON fields, output formats) is a named constant in that file;
nothing outside it spells one."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..model import Account, AccountStatus, Candidate, Capture, SessionSeen, Window


class Provider(Protocol):
    name: str
    tool: str                # the provider's tool as users know it, for messages
    default_plan: str        # proposed at join when the sign-in check names no plan
    shared: tuple[str, ...]  # what every account shares through the hub; "x/" names a folder
    not_copied: tuple[str, ...]  # of those, what join never copies in: live state, keys
    usage_timeout: float     # seconds read_usage waits at most

    def parse_capture(self, raw: dict) -> dict[str, Window]:
        """Turn the provider's own usage payload into windows keyed by the plan's window names.
        Return {} when the payload carries no usage."""

    def session_of(self, raw: object) -> SessionSeen | None:
        """The session (id, folder, transcript) the usage payload `raw` describes, else None."""

    def hook_session(self, hook: str, raw: object) -> SessionSeen | None:
        """For fairshare's hook named `hook` (`fairshare hook <name>`): the session its input
        `raw` describes, when the input is the event that hook is wired for; else None."""

    def current_session(self) -> str | None:
        """The id of the session this process runs inside (a command run by it), else None."""

    def session_pid(self) -> int | None:
        """The process id of that session, when the provider says, else None."""

    def resume_args(self, session_id: str) -> list[str]:
        """Arguments that continue the session `session_id` in a new one."""

    def background(self, account: Account, name: str, prompt: str
                   ) -> tuple[list[str], dict[str, str]]:
        """The command and the clean environment (as `launch`) that start a session of
        `account` with no terminal, named `name`, whose first message is `prompt`."""

    def transcript_text(self, path: Path) -> list[tuple[str, str]] | None:
        """A session transcript's main conversation, oldest first, as ("user" | "assistant",
        text) pairs: words only. [] when it has none; None when it cannot be read."""

    def launch(self, account: Account) -> tuple[list[str], dict[str, str]]:
        """The command and the complete environment that start this provider as `account`: the
        provider's clean environment (see dropped_login_vars) plus the account's own variable."""

    def login_command(self, config_dir: Path) -> tuple[list[str], dict[str, str]]:
        """The command and environment of the provider's own interactive sign-in for the account
        folder `config_dir`. fairshare runs it and never sees what it stores."""

    def logout_command(self, config_dir: Path) -> tuple[list[str], dict[str, str]]:
        """The same for signing that folder out."""

    def session_dir(self) -> Path | None:
        """The account folder of the session this process runs in (as a usage hook), else None."""

    def dropped_login_vars(self) -> list[str]:
        """Names (never values) of variables set in this process that could override an account's
        own login and that launch() leaves out of the environment."""

    def passed_session_vars(self) -> list[str]:
        """Inside a running session of this provider: names (never values) of other variables
        that look like session state and that launch() passes through. Empty otherwise."""

    def discover(self, home: Path) -> list[Candidate]:
        """The user's own folders under `home`, signed in or not, for setup to show. Must not
        read a login."""

    def account_status(self, account: Account) -> AccountStatus | None:
        """Ask the provider whether `account` is signed in, with what kind of login, under which
        identity and plan. None when the question could not be answered (never raise)."""

    def read_usage(self, account: Account, now: float) -> dict[str, Window] | str | None:
        """Ask the provider's own tool for `account`'s usage now, without a model call: the
        windows it read, keyed as parse_capture keys them (per-model weeks under
        `model.model_window`); else a short, value-free reason, one of `model.USAGE_*` (a detail
        may follow USAGE_NO_ANSWER after a colon); None when the account turned the probe off.
        Never raises."""

    def find_binary(self) -> str | None:
        """The provider's command-line tool on PATH, or None."""

    def version(self) -> str | None:
        """The tool's version line, or None when it could not be read (never raise)."""

    def state_file(self, folder: Path, home: Path) -> Path:
        """The provider's own state file that the user's folder `folder` uses."""

    def hold_state(self, state: Path):
        """A context manager holding the lock the provider itself takes while it writes the
        state file `state`; raises OSError when it cannot be had."""

    def trusts(self, config_dir: Path, path: Path) -> bool | None:
        """Whether the account folder `config_dir` lets the tool work in the canonical folder
        `path` without asking (by the provider's own lookup rule); None when it cannot tell."""

    def trust(self, config_dir: Path, path: Path, backups: Path) -> Path:
        """Record that trust as the provider's own dialog would, backing the file up into
        `backups`, and return the folder the entry names. Raises OSError or ValueError and
        writes nothing when it cannot."""

    def check_state(self, config_dir: Path) -> tuple[bool, str]:
        """For doctor: whether the account's state file still has the layout fairshare relies
        on, and one line saying so (no paths, no values)."""

    def merge_state(self, dest: dict, src: dict) -> tuple[dict, list[str]]:
        """`dest` (a new account's state) plus the few keys join may carry over from `src` (a
        user's folder's): never identity, tokens or account fields. Also returns what was added,
        as names and counts for the user, never values."""

    def masked(self, item: str, text: str) -> str:
        """A shared item's text fit to show the user in a diff, with any value that may be a
        secret (such as a variable's value) hidden."""

    def capture_file(self, shared: Path) -> Path:
        """The settings file, among the hub's shared items, in which the usage hook lives."""

    def wired(self, text: str | None) -> Capture | None:
        """The fairshare capture hook found in that file's text, else None."""

    def repath(self, text: str, old: str, new: str) -> tuple[str | None, list[str]]:
        """That file's text with every string value naming the folder `old` renamed to `new`, and
        where each was (key paths only); (None, []) when nothing names it. Every other byte stays.
        Raises ValueError for a text it cannot change safely."""

    def wire(self, text: str | None, command: str) -> str | None:
        """New text for that file so that it records usage through the shell command `command`
        and then runs the user's own hook, redraws on a timer, and runs fairshare's own hooks
        (`<command> hook <name>`); None when it already does. Every byte outside fairshare's own
        entries stays, and no other hook is changed, moved or removed. Raises ValueError for a
        text it cannot change safely."""

    def hooked(self, text: str | None) -> tuple[str, ...] | None:
        """The fairshare command all of fairshare's own hooks in that file's text run, else
        None (one is missing)."""
