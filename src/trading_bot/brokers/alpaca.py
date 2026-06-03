from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from trading_bot.brokers.base import BrokerError
from trading_bot.config import AppConfig, first_env


class AlpacaCredentialsError(BrokerError):
    """Raised when Alpaca credentials are missing."""


@dataclass(frozen=True)
class AlpacaClient:
    base_url: str
    key_id: str
    secret_key: str
    timeout_seconds: float = 15.0

    @classmethod
    def from_config(cls, config: AppConfig) -> "AlpacaClient":
        key_id = first_env("ALPACA_API_KEY_ID", "APCA_API_KEY_ID")
        secret_key = first_env("ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise AlpacaCredentialsError(
                "Missing Alpaca paper credentials. Set ALPACA_API_KEY_ID and "
                "ALPACA_API_SECRET_KEY in .env."
            )

        base_url = str(
            config.get(
                "alpaca",
                "active_trading_base_url",
                default=config.get("alpaca", "paper_base_url", default="https://paper-api.alpaca.markets"),
            )
        ).rstrip("/")
        return cls(base_url=base_url, key_id=key_id, secret_key=secret_key)

    def get_account(self) -> dict[str, Any]:
        return self._get("/v2/account")

    def get_clock(self) -> dict[str, Any]:
        return self._get("/v2/clock")

    def get_positions(self) -> list[dict[str, Any]]:
        data = self._get("/v2/positions")
        if not isinstance(data, list):
            raise BrokerError("Expected Alpaca positions response to be a list")
        return data

    def get_orders(self, *, status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
        data = self._get("/v2/orders", params={"status": status, "limit": str(limit)})
        if not isinstance(data, list):
            raise BrokerError("Expected Alpaca orders response to be a list")
        return data

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }
        try:
            response = httpx.get(url, headers=headers, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BrokerError(
                f"Alpaca request failed: {exc.response.status_code} {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"Alpaca request failed: {exc}") from exc
        return response.json()

