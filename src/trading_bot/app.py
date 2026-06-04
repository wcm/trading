from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from trading_bot.config import load_config, load_env_file, resolve_path
from trading_bot.logging_config import configure_logging
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import init_db


def bootstrap(args: argparse.Namespace) -> tuple[Any, logging.Logger, Path, KillSwitch, DiscordNotifier]:
    loaded_env_count = load_env_file(args.env)
    config = load_config(args.settings)

    log_dir = resolve_path(config.get("runtime", "log_dir", default="logs"))
    logger = configure_logging(log_dir)
    logger.info("Loaded %s values from env file", loaded_env_count)
    logger.info("Mode=%s broker=%s", config.mode, config.broker)

    db_path = resolve_path(config.get("storage", "sqlite_path", default="data/trading_bot.sqlite3"))
    init_db(db_path)

    kill_switch_path = resolve_path(config.get("runtime", "kill_switch_path", default="KILL_SWITCH"))
    kill_switch = KillSwitch(kill_switch_path)
    if kill_switch.is_active():
        logger.warning("Kill switch is active at %s; execution must remain disabled", kill_switch.path)
    else:
        logger.info("Kill switch is not active")

    notifier = DiscordNotifier.from_config(config)
    return config, logger, db_path, kill_switch, notifier
