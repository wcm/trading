from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    settings_path: Path
    values: dict[str, Any]

    @property
    def mode(self) -> str:
        return str(self.values.get("mode", "paper"))

    @property
    def broker(self) -> str:
        return str(self.values.get("broker", "alpaca"))

    def get(self, *path: str, default: Any = None) -> Any:
        current: Any = self.values
        for part in path:
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_path(path_value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return (base or project_root() / path).resolve()


def load_env_file(path: str | Path = ".env") -> int:
    env_path = resolve_path(path)
    if not env_path.exists():
        return 0

    loaded = 0
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
            loaded += 1
    return loaded


def load_config(settings_path: str | Path = "config/settings.yaml") -> AppConfig:
    path = resolve_path(settings_path)
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        values = yaml.safe_load(handle) or {}
    if not isinstance(values, dict):
        raise ConfigError(f"Settings file must contain a mapping: {path}")

    return AppConfig(settings_path=path, values=values)


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None

