from __future__ import annotations

import unittest
from unittest.mock import patch

from trading_bot.notifications.discord import DiscordNotifier


class FakeResponse:
    def raise_for_status(self) -> None:
        return None


class DiscordNotifierTests(unittest.TestCase):
    def test_send_uses_webhook_default_name(self) -> None:
        notifier = DiscordNotifier("https://discord.test/webhook")

        with patch("trading_bot.notifications.discord.httpx.post", return_value=FakeResponse()) as post:
            result = notifier.send("hello")

        self.assertTrue(result.ok)
        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload, {"content": "hello"})
        self.assertNotIn("username", payload)


if __name__ == "__main__":
    unittest.main()
