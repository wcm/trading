from __future__ import annotations

import unittest

from trading_bot.llm.openai_client import OpenAIClient


class CapturingOpenAIClient(OpenAIClient):
    captured_payload: dict | None = None

    def _post(self, path: str, payload: dict) -> dict:
        type(self).captured_payload = payload
        return {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"action":"skip"}',
                        }
                    ]
                }
            ]
        }


class OpenAIClientTests(unittest.TestCase):
    def test_adds_reasoning_effort_to_responses_payload(self) -> None:
        client = CapturingOpenAIClient(
            api_key="test-key",
            model="gpt-5.5",
            reasoning_effort="high",
        )

        decision, _raw = client.create_trading_decision(
            prompt_text="Return JSON.",
            decision_packet={"symbol": "AAPL"},
        )

        self.assertEqual(decision, {"action": "skip"})
        assert CapturingOpenAIClient.captured_payload is not None
        self.assertEqual(
            CapturingOpenAIClient.captured_payload["reasoning"],
            {"effort": "high"},
        )

    def test_omits_reasoning_when_effort_is_not_configured(self) -> None:
        CapturingOpenAIClient.captured_payload = None
        client = CapturingOpenAIClient(api_key="test-key", model="gpt-5.5")

        client.create_trading_decision(
            prompt_text="Return JSON.",
            decision_packet={"symbol": "AAPL"},
        )

        assert CapturingOpenAIClient.captured_payload is not None
        self.assertNotIn("reasoning", CapturingOpenAIClient.captured_payload)


if __name__ == "__main__":
    unittest.main()
