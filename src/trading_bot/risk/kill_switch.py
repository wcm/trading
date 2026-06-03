from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KillSwitch:
    path: Path

    def is_active(self) -> bool:
        return self.path.exists()

