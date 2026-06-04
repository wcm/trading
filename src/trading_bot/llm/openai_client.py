from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from trading_bot.config import AppConfig
from trading_bot.llm.schemas import DECISION_RESPONSE_JSON_SCHEMA


class OpenAIClientError(RuntimeError):
    """Raised when the OpenAI decision request fails."""


@dataclass(frozen=True)
class OpenAIClient:
    api_key: str
    model: str
    reasoning_effort: str | None = None
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0

    @classmethod
    def from_config(cls, config: AppConfig) -> "OpenAIClient":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise OpenAIClientError("Missing OPENAI_API_KEY in .env")

        model = os.environ.get("OPENAI_MODEL") or str(
            config.get("decision_engine", "model", default="gpt-5.5")
        )
        reasoning_effort = os.environ.get("OPENAI_REASONING_EFFORT") or config.get(
            "decision_engine",
            "reasoning_effort",
            default=None,
        )
        base_url = str(
            config.get("decision_engine", "openai_base_url", default="https://api.openai.com/v1")
        ).rstrip("/")
        return cls(
            api_key=api_key,
            model=model,
            reasoning_effort=str(reasoning_effort) if reasoning_effort else None,
            base_url=base_url,
        )

    def create_trading_decision(
        self,
        *,
        prompt_text: str,
        decision_packet: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request_payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": prompt_text,
                },
                {
                    "role": "user",
                    "content": (
                        "Return a JSON trading decision for this read-only decision packet:\n"
                        + json.dumps(decision_packet, sort_keys=True)
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "trading_decision_v1",
                    "description": "Read-only trading decision for a put credit spread bot.",
                    "strict": True,
                    "schema": DECISION_RESPONSE_JSON_SCHEMA,
                }
            },
        }
        if self.reasoning_effort:
            request_payload["reasoning"] = {"effort": self.reasoning_effort}

        response_json = self._post("/responses", request_payload)
        output_text = _extract_output_text(response_json)
        try:
            decision = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise OpenAIClientError(f"OpenAI response was not valid JSON: {output_text[:500]}") from exc
        if not isinstance(decision, dict):
            raise OpenAIClientError("OpenAI response JSON must be an object")
        return decision, response_json

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenAIClientError(
                f"OpenAI request failed: {exc.response.status_code} {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenAIClientError(f"OpenAI request failed: {exc}") from exc
        data = response.json()
        if not isinstance(data, dict):
            raise OpenAIClientError("OpenAI response must be a JSON object")
        return data


def _extract_output_text(response_json: dict[str, Any]) -> str:
    direct = response_json.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    output = response_json.get("output")
    if not isinstance(output, list):
        raise OpenAIClientError("OpenAI response did not include output text")

    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("type") == "refusal" or content_item.get("refusal"):
                raise OpenAIClientError(f"OpenAI refused decision request: {content_item}")
            text = content_item.get("text")
            if isinstance(text, str):
                chunks.append(text)

    output_text = "".join(chunks).strip()
    if not output_text:
        raise OpenAIClientError("OpenAI response output text was empty")
    return output_text
