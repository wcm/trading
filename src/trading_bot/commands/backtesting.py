from __future__ import annotations

import argparse
import itertools
import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal

from trading_bot.backtesting.bars import PriceBar, load_historical_bars
from trading_bot.backtesting.grid import GridBacktestConfig, result_summary_row, run_grid_backtest
from trading_bot.backtesting.reports import trades_to_rows, write_csv, write_markdown_table
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import AppConfig, load_config, load_env_file, resolve_path
from trading_bot.logging_config import configure_logging
from trading_bot.utils.artifacts import write_json_artifact


def run_grid_backtest_command(args: argparse.Namespace) -> int:
    config, logger = _load_backtest_context(args)
    logger.info("Starting grid backtest")
    bars = _load_bars_for_args(args, config, logger)
    if not bars:
        logger.error("No historical bars loaded")
        return 1

    backtest_config = _grid_config_from_args(args, config)
    result = run_grid_backtest(bars, backtest_config)
    _log_result_summary(logger, result.metrics)

    if args.json_output:
        write_json_artifact(args.json_output, result.to_dict(), logger, "grid backtest")
    if args.trades_csv:
        write_csv(args.trades_csv, trades_to_rows([trade.to_dict() for trade in result.trades]))
        logger.info("Wrote grid backtest trades CSV to %s", args.trades_csv)
    return 0


def run_grid_sweep_command(args: argparse.Namespace) -> int:
    config, logger = _load_backtest_context(args)
    logger.info("Starting grid parameter sweep")
    bars = _load_bars_for_args(args, config, logger)
    if not bars:
        logger.error("No historical bars loaded")
        return 1

    rows = []
    results = []
    scale_factors = (
        _decimal_list(args.adaptive_scale_factors)
        if args.adaptive_scale_factors
        else [None]
    )
    for spacing, notional, inventory, scale_factor in itertools.product(
        _decimal_list(args.grid_spacing_pcts),
        _decimal_list(args.base_order_notionals),
        _decimal_list(args.max_inventory_values),
        scale_factors,
    ):
        run_args = argparse.Namespace(**vars(args))
        run_args.grid_spacing_pct = str(spacing)
        run_args.base_order_notional = str(notional)
        run_args.max_inventory_value = str(inventory)
        if scale_factor is not None:
            run_args.adaptive_scale_factor = str(scale_factor)
            run_args.adaptive_sizing = scale_factor > 0
        backtest_config = _grid_config_from_args(run_args, config)
        result = run_grid_backtest(bars, backtest_config)
        rows.append(result_summary_row(result))
        results.append(result.to_dict())

    rows.sort(
        key=lambda row: (
            Decimal(str(row["spacing_pct"])),
            Decimal(str(row["order_notional"])),
            Decimal(str(row["max_inventory_limit"])),
            Decimal(str(row["adaptive_scale_factor"])),
        )
    )
    _log_sweep_table(logger, rows)

    if args.csv_output:
        write_csv(args.csv_output, rows)
        logger.info("Wrote grid sweep CSV to %s", args.csv_output)
    if args.markdown_output:
        write_markdown_table(args.markdown_output, rows)
        logger.info("Wrote grid sweep Markdown to %s", args.markdown_output)
    if args.json_output:
        write_json_artifact(
            args.json_output,
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "bar_count": len(bars),
                "rows": rows,
                "results": results,
            },
            logger,
            "grid sweep",
        )
    return 0


def _load_backtest_context(args: argparse.Namespace) -> tuple[AppConfig, logging.Logger]:
    loaded_env_count = load_env_file(args.env)
    config = load_config(args.settings)
    logger = configure_logging(resolve_path(config.get("runtime", "log_dir", default="logs")))
    logger.info("Loaded %s values from env file", loaded_env_count)
    logger.info("Mode=%s broker=%s", config.mode, config.broker)
    return config, logger


def _load_bars_for_args(args: argparse.Namespace, config: AppConfig, logger: logging.Logger) -> list[PriceBar]:
    source = str(args.data_source)
    alpaca = None
    if source == "alpaca":
        try:
            alpaca = AlpacaClient.from_config(config)
        except AlpacaCredentialsError as exc:
            raise SystemExit(str(exc)) from exc
    symbol = _arg_or_config(args.symbol, config.get("grid_strategy", "symbol", default="TQQQ"))
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    bars = load_historical_bars(
        config=config,
        alpaca=alpaca,
        symbol=symbol,
        timeframe=args.timeframe,
        start=start,
        end=end,
        source=source,
        cache_dir=args.cache_dir,
        feed=args.feed,
        use_cache=not args.no_cache,
    )
    logger.info(
        "Loaded %s %s bars for %s from %s: %s to %s",
        len(bars),
        args.timeframe,
        symbol,
        source,
        bars[0].timestamp if bars else "-",
        bars[-1].timestamp if bars else "-",
    )
    return bars


def _grid_config_from_args(args: argparse.Namespace, config: AppConfig) -> GridBacktestConfig:
    symbol = _arg_or_config(args.symbol, config.get("grid_strategy", "symbol", default="TQQQ"))
    strategy_capital = _decimal_arg_or_config(
        args.strategy_capital,
        config.get("grid_risk", "strategy_capital", default=10_000),
    )
    adaptive_scale_factor = _optional_decimal_arg_or_config(
        args.adaptive_scale_factor,
        config.get("adaptive_sizing", "scale_factor", default=0),
    ) or Decimal("0")
    adaptive_sizing_enabled = (
        bool(args.adaptive_sizing)
        or bool(config.get("adaptive_sizing", "enabled", default=False))
        or (args.adaptive_scale_factor is not None and adaptive_scale_factor > 0)
    )
    return GridBacktestConfig(
        symbol=symbol,
        starting_cash=strategy_capital,
        grid_spacing_pct=_decimal_arg_or_config(
            args.grid_spacing_pct,
            config.get("grid_strategy", "grid_spacing_pct", default=5.0),
        ),
        base_order_notional=_decimal_arg_or_config(
            args.base_order_notional,
            config.get("grid_strategy", "base_order_notional", default=500),
        ),
        max_buy_levels_below_anchor=int(
            args.max_buy_levels_below_anchor
            if args.max_buy_levels_below_anchor is not None
            else config.get("grid_strategy", "max_buy_levels_below_anchor", default=16)
        ),
        max_inventory_value=_decimal_arg_or_config(
            args.max_inventory_value,
            config.get("grid_risk", "max_inventory_value", default=8_000),
        ),
        cash_reserve=_decimal_arg_or_config(
            args.cash_reserve,
            config.get("grid_risk", "cash_reserve", default=2_000),
        ),
        max_unrealized_loss=_optional_decimal_arg_or_config(
            args.max_unrealized_loss,
            config.get("grid_risk", "max_unrealized_loss", default=1_200),
        ),
        pause_new_buys_after_consecutive_down_levels=_optional_int_arg_or_config(
            args.pause_new_buys_after_consecutive_down_levels,
            config.get("grid_risk", "pause_new_buys_after_consecutive_down_levels", default=5),
        ),
        recenter_up_pct=_optional_decimal_arg_or_config(
            args.recenter_up_pct,
            config.get("grid_strategy", "recenter_up_pct", default=5.0),
        ),
        adaptive_sizing_enabled=adaptive_sizing_enabled,
        adaptive_scale_factor=adaptive_scale_factor,
        adaptive_max_order_multiplier=_decimal_arg_or_config(
            args.adaptive_max_order_multiplier,
            config.get("adaptive_sizing", "max_order_multiplier", default=1),
        ),
        max_single_order_notional=_optional_decimal_arg_or_config(
            args.max_single_order_notional,
            config.get("adaptive_sizing", "max_single_order_notional", default=None),
        ),
        allow_fractional_shares=bool(args.allow_fractional_shares),
    )


def _arg_or_config(value: str | None, fallback: object) -> str:
    if value is not None:
        return str(value).strip().upper()
    return str(fallback).strip().upper()


def _decimal_arg_or_config(value: object, fallback: object) -> Decimal:
    raw = fallback if value is None else value
    return Decimal(str(raw))


def _optional_decimal_arg_or_config(value: object, fallback: object) -> Decimal | None:
    raw = fallback if value is None else value
    if raw is None or str(raw).strip().lower() in {"", "none", "null", "off"}:
        return None
    return Decimal(str(raw))


def _optional_int_arg_or_config(value: object, fallback: object) -> int | None:
    raw = fallback if value is None else value
    if raw is None or str(raw).strip().lower() in {"", "none", "null", "off"}:
        return None
    return int(raw)


def _decimal_list(value: str) -> list[Decimal]:
    result = []
    for raw in value.split(","):
        text = raw.strip()
        if text:
            result.append(Decimal(text))
    if not result:
        raise ValueError("Expected at least one comma-separated decimal value")
    return result


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _log_result_summary(logger: logging.Logger, metrics: dict[str, object]) -> None:
    logger.info(
        (
            "Grid backtest complete: total_pnl=%s return=%s%% max_drawdown=%s%% "
            "buys=%s sells=%s max_inventory=%s worst_unrealized=%s open_lots=%s"
        ),
        metrics["total_pnl"],
        metrics["total_return_pct"],
        metrics["max_drawdown_pct"],
        metrics["buy_count"],
        metrics["sell_count"],
        metrics["max_inventory_value"],
        metrics["worst_unrealized_loss"],
        metrics["open_lot_count"],
    )


def _log_sweep_table(logger: logging.Logger, rows: list[dict[str, object]]) -> None:
    logger.info(
        (
            "spacing order max_inv scale total_pnl return_pct max_dd buys sells "
            "worst_unrealized paused open_lots"
        )
    )
    for row in rows:
        logger.info(
            "%s %s %s %s %s %s %s %s %s %s %s %s recenters=%s",
            row["spacing_pct"],
            row["order_notional"],
            row["max_inventory_limit"],
            row["adaptive_scale_factor"],
            row["total_pnl"],
            row["total_return_pct"],
            row["max_drawdown_pct"],
            row["buys"],
            row["sells"],
            row["worst_unrealized_loss"],
            row["paused_days"],
            row["open_lots"],
            row["recenters"],
        )
