from __future__ import annotations

import unittest

from trading_bot.allocation import build_allocation_summary
from trading_bot.config import load_config


def open_artifact(
    *,
    symbol: str,
    decision_id: int,
    candidate_id: str,
    confidence: float,
    max_profit: str,
    max_loss: str,
) -> dict:
    return {
        "decision_id": decision_id,
        "accepted": True,
        "validator_errors": [],
        "decision": {
            "action": "open",
            "symbol": symbol,
            "candidate_id": candidate_id,
            "quantity": 1,
            "limit_price": "-1.00",
            "confidence": confidence,
            "decision_reason": f"{symbol} test open",
        },
        "packet": {
            "option_scan": {
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "underlying_symbol": symbol,
                        "net_credit": "1.00",
                        "max_profit": max_profit,
                        "max_loss": max_loss,
                    }
                ]
            }
        },
    }


class AllocationTests(unittest.TestCase):
    def test_allocator_selects_highest_confidence_open_first(self) -> None:
        config = load_config("config/settings.yaml")
        summary = build_allocation_summary(
            config,
            [
                open_artifact(
                    symbol="AAPL",
                    decision_id=1,
                    candidate_id="AAPL-CANDIDATE",
                    confidence=0.74,
                    max_profit="105",
                    max_loss="395",
                ),
                open_artifact(
                    symbol="MSFT",
                    decision_id=2,
                    candidate_id="MSFT-CANDIDATE",
                    confidence=0.76,
                    max_profit="101",
                    max_loss="399",
                ),
            ],
        )

        self.assertEqual(summary["accepted_open_count"], 2)
        self.assertEqual(summary["selected_open"]["symbol"], "MSFT")
        self.assertEqual(summary["selected_open"]["max_contracts_under_open_risk"], 12)

    def test_allocator_returns_no_selection_without_accepted_opens(self) -> None:
        config = load_config("config/settings.yaml")
        summary = build_allocation_summary(
            config,
            [
                {
                    "accepted": True,
                    "decision": {"action": "skip"},
                    "packet": {"option_scan": {"candidates": []}},
                }
            ],
        )

        self.assertEqual(summary["accepted_open_count"], 0)
        self.assertIsNone(summary["selected_open"])

    def test_allocator_skips_open_that_exceeds_open_risk_budget(self) -> None:
        config = load_config("config/settings.yaml")
        summary = build_allocation_summary(
            config,
            [
                open_artifact(
                    symbol="RICH",
                    decision_id=1,
                    candidate_id="RICH-CANDIDATE",
                    confidence=0.90,
                    max_profit="1000",
                    max_loss="6000",
                ),
                open_artifact(
                    symbol="OK",
                    decision_id=2,
                    candidate_id="OK-CANDIDATE",
                    confidence=0.80,
                    max_profit="800",
                    max_loss="4000",
                ),
            ],
        )

        self.assertEqual(summary["accepted_open_count"], 2)
        self.assertEqual(summary["selected_open"]["symbol"], "OK")


if __name__ == "__main__":
    unittest.main()
