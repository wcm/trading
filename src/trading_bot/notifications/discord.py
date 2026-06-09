from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from trading_bot.config import AppConfig


@dataclass(frozen=True)
class NotificationResult:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class DiscordNotifier:
    webhook_url: str | None
    timeout_seconds: float = 10.0

    @classmethod
    def from_config(cls, config: AppConfig) -> "DiscordNotifier":
        env_var = str(config.get("notifications", "webhook_env_var", default="DISCORD_WEBHOOK_URL"))
        return cls(webhook_url=os.environ.get(env_var))

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, content: str) -> NotificationResult:
        if not self.webhook_url:
            return NotificationResult(ok=False, error="DISCORD_WEBHOOK_URL is not configured")

        try:
            response = httpx.post(
                self.webhook_url,
                json={"content": content},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return NotificationResult(
                ok=False,
                error=f"Discord webhook failed: {exc.response.status_code} {exc.response.text}",
            )
        except httpx.HTTPError as exc:
            return NotificationResult(ok=False, error=f"Discord webhook failed: {exc}")

        return NotificationResult(ok=True)
