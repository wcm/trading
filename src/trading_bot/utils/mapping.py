from __future__ import annotations

from typing import Any


def nested_get(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None
