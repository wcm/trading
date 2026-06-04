from __future__ import annotations

import json
import logging
from typing import Any

from trading_bot.config import resolve_path


def write_json_artifact(
    output_path_value: str,
    artifact: dict[str, Any],
    logger: logging.Logger,
    label: str,
) -> None:
    output_path = resolve_path(output_path_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Wrote %s JSON to %s", label, output_path)
