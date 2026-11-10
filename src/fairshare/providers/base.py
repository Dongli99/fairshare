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
    default_plan: str        # offered first at setup; the user confirms

    def parse_capture(self, raw: dict) -> dict[str, Window]:
        """Turn the provider's own usage payload into windows keyed by the plan's window names.
        Return {} when the payload carries no usage."""

    def launch(self, account: Account) -> tuple[list[str], dict[str, str]]:
        """The command and the complete environment that start this provider as `account`: the
        provider's clean environment (see dropped_login_vars) plus the account's own variable."""

    def dropped_login_vars(self) -> list[str]:
        """Names (never values) of variables set in this process that could override an account's
        own login and that launch() leaves out of the environment."""

    def discover(self, home: Path) -> list[Candidate]:
        """Accounts already set up under `home`, for setup to show. Must not read a login."""

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

    def capture_file(self, account: Account) -> Path:
        """The settings file in which the account's usage hook lives."""

    def wired(self, text: str | None, account_id: str) -> Capture | None:
        """The fairshare capture hook found in that file's text for `account_id`, else None."""

    def wire(self, files: dict[Path, str | None], accounts: list[Account],
             command: str) -> dict[Path, str]:
        """New text for each settings file that must change so every account records its usage
        through the shell command `command`. `files` maps path -> current text (None: absent)."""

    def unwire(self, files: dict[Path, str | None], accounts: list[Account]) -> dict[Path, str]:
        """The reverse of wire(): new text for each file that carries fairshare's change."""
