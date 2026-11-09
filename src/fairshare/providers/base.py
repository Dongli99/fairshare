"""What a provider adapter must do. Add a provider = one new file implementing this."""
from __future__ import annotations

from typing import Protocol

from ..model import Account, Window


class Provider(Protocol):
    name: str

    def parse_capture(self, raw: dict) -> dict[str, Window]:
        """Turn the provider's own usage payload into windows keyed by the plan's window names.
        Return {} when the payload carries no usage."""

    def launch(self, account: Account) -> tuple[list[str], dict[str, str]]:
        """The command and extra environment that start this provider as `account`."""
