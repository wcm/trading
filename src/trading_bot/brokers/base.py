from __future__ import annotations

from typing import Any, Protocol


class BrokerError(RuntimeError):
    """Raised when a broker request fails."""


class BrokerClient(Protocol):
    def get_account(self) -> dict[str, Any]:
        """Return account details."""

    def get_clock(self) -> dict[str, Any]:
        """Return market clock details."""

    def get_positions(self) -> list[dict[str, Any]]:
        """Return current positions."""

