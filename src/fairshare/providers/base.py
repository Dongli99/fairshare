"""What a provider adapter must do. Add a provider = one new file implementing this."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..model import Account, AccountStatus, Candidate, Window


class Provider(Protocol):
    name: str
    default_plan: str        # offered first at setup; the user confirms

    def parse_capture(self, raw: dict) -> dict[str, Window]:
        """Turn the provider's own usage payload into windows keyed by the plan's window names.
        Return {} when the payload carries no usage."""

    def launch(self, account: Account) -> tuple[list[str], dict[str, str]]:
        """The command and extra environment that start this provider as `account`."""

    def discover(self, home: Path) -> list[Candidate]:
        """Accounts already set up under `home`, for setup to show. Must not read a login."""

    def account_status(self, account: Account) -> AccountStatus | None:
        """Ask the provider whether `account` is signed in, with what kind of login, under which
        identity and plan. None when the question could not be answered (never raise)."""
