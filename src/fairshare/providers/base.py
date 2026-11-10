"""What a provider adapter must do. Add a provider = one new file implementing this.
Every fact about the provider that its vendor may change (binary name, arguments, environment
variables, file and folder names, JSON fields, output formats) is a named constant in that file;
nothing outside it spells one."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..model import Account, AccountStatus, Candidate, Capture, Window


class Provider(Protocol):
    name: str
    default_plan: str        # proposed at join when the sign-in check names no plan
    shared: tuple[str, ...]  # what every account shares through the hub; "x/" names a folder

    def parse_capture(self, raw: dict) -> dict[str, Window]:
        """Turn the provider's own usage payload into windows keyed by the plan's window names.
        Return {} when the payload carries no usage."""

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

    def login_present(self, account: Account) -> bool:
        """Whether the account's folder holds a login, by existence only: the fallback when
        account_status gives no answer. Must not open the login."""

    def find_binary(self) -> str | None:
        """The provider's command-line tool on PATH, or None."""

    def version(self) -> str | None:
        """The tool's version line, or None when it could not be read (never raise)."""

    def state_file(self, folder: Path, home: Path) -> Path:
        """The provider's own state file that the user's folder `folder` uses."""

    def state_lock(self, state: Path) -> Path:
        """The lock the provider itself takes while it writes the state file `state`."""

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

    def wire(self, text: str | None, command: str) -> str | None:
        """New text for that file so that it records usage through the shell command `command`
        and then runs the user's own hook; None when it already does. Every byte outside the
        hook stays. Raises ValueError for a text it cannot change safely."""
