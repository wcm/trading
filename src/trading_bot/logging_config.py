from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path, *, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "bot.log"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return logging.getLogger("trading_bot")

