from __future__ import annotations

import argparse


def symbols_from_args_or_config(args: argparse.Namespace, config) -> list[str]:
    if args.symbols:
        raw_symbols = args.symbols.split(",")
    else:
        raw_symbols = config.get("strategy", "preferred_symbols", default=[])
    return [str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()]


def watchlist_symbols_from_args_or_config(args: argparse.Namespace, config) -> list[str]:
    if args.symbols:
        raw_symbols = args.symbols.split(",")
    else:
        raw_symbols = config.get("strategy", "watchlist", default=[])
    return [str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()]
