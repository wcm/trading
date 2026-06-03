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
    data_base_url: str
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
        data_base_url = str(
            config.get("alpaca", "data_base_url", default="https://data.alpaca.markets")
        ).rstrip("/")
        return cls(base_url=base_url, data_base_url=data_base_url, key_id=key_id, secret_key=secret_key)

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

    def get_latest_stock_bars(self, symbols: list[str], *, feed: str = "iex") -> dict[str, Any]:
        data = self._get_data(
            "/v2/stocks/bars/latest",
            params={"symbols": ",".join(symbols), "feed": feed},
        )
        bars = data.get("bars", data)
        if not isinstance(bars, dict):
            raise BrokerError("Expected Alpaca latest bars response to contain a bars mapping")
        return bars

    def get_option_contracts(
        self,
        *,
        underlying_symbols: list[str],
        expiration_date_gte: str,
        expiration_date_lte: str,
        option_type: str = "put",
        status: str = "active",
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        params = {
            "underlying_symbols": ",".join(underlying_symbols),
            "expiration_date_gte": expiration_date_gte,
            "expiration_date_lte": expiration_date_lte,
            "type": option_type,
            "status": status,
            "limit": str(limit),
        }
        contracts: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            request_params = dict(params)
            if page_token:
                request_params["page_token"] = page_token
            data = self._get("/v2/options/contracts", params=request_params)
            page_contracts = data.get("option_contracts", [])
            if not isinstance(page_contracts, list):
                raise BrokerError("Expected Alpaca option contracts response to contain option_contracts")
            contracts.extend(page_contracts)
            page_token = data.get("next_page_token")
            if not page_token:
                return contracts

    def get_option_snapshots(
        self,
        symbols: list[str],
        *,
        feed: str = "opra",
        chunk_size: int = 100,
    ) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        for index in range(0, len(symbols), chunk_size):
            chunk = symbols[index : index + chunk_size]
            data = self._get_data(
                "/v1beta1/options/snapshots",
                params={"symbols": ",".join(chunk), "feed": feed, "limit": str(chunk_size)},
            )
            page_snapshots = data.get("snapshots", data)
            if not isinstance(page_snapshots, dict):
                raise BrokerError("Expected Alpaca option snapshots response to contain a snapshots mapping")
            snapshots.update(page_snapshots)
        return snapshots

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._request("GET", f"{self.base_url}{path}", params=params)

    def _get_data(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._request("GET", f"{self.data_base_url}{path}", params=params)

    def _request(self, method: str, url: str, params: dict[str, str] | None = None) -> Any:
        headers = {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BrokerError(
                f"Alpaca request failed: {exc.response.status_code} {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"Alpaca request failed: {exc}") from exc
        return response.json()
