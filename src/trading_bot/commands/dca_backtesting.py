from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from decimal import Decimal

from trading_bot.backtesting.bars import load_historical_bars
from trading_bot.backtesting.dca import run_dca_backtest
from trading_bot.backtesting.reports import write_csv
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import AppConfig, load_config, load_env_file, resolve_path
from trading_bot.dca.strategy import dca_config_from_app_config
from trading_bot.logging_config import configure_logging
from trading_bot.utils.artifacts import write_json_artifact


def run_dca_backtest_command(args: argparse.Namespace) -> int:
    loaded_env_count = load_env_file(args.env)
    config = load_config(args.settings)
    logger = configure_logging(
        resolve_path(config.get("runtime", "log_dir", default="logs"))
    )
    logger.info("Loaded %s values from env file", loaded_env_count)
    logger.info("Starting DCA backtest")

    dca_config = _dca_config_from_args(args, config)
    alpaca = None
    if args.data_source == "alpaca":
        try:
            alpaca = AlpacaClient.from_config(config)
        except AlpacaCredentialsError as exc:
            logger.error("%s", exc)
            return 1
    bars = load_historical_bars(
        config=config,
        alpaca=alpaca,
        symbol=dca_config.symbol,
        timeframe=args.timeframe,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        source=args.data_source,
        cache_dir=args.cache_dir,
        feed=args.feed,
        adjustment=args.adjustment,
        use_cache=not args.no_cache,
    )
    if not bars:
        logger.error("No historical bars loaded")
        return 1

    result = run_dca_backtest(bars, dca_config)
    metrics = result.metrics
    logger.info(
        "DCA backtest complete: contributed=%s final_value=%s gain=%s "
        "return=%s%% purchases=%s",
        metrics["total_contributed"],
        metrics["final_value"],
        metrics["investment_gain"],
        metrics["simple_return_pct"],
        metrics["contribution_count"],
    )
    if args.json_output:
        write_json_artifact(args.json_output, result.to_dict(), logger, "DCA backtest")
    if args.purchases_csv:
        write_csv(
            args.purchases_csv,
            [purchase.to_dict() for purchase in result.purchases],
        )
        logger.info("Wrote DCA purchases CSV to %s", args.purchases_csv)
    return 0


def _dca_config_from_args(args: argparse.Namespace, config: AppConfig):
    base = dca_config_from_app_config(config)
    return replace(
        base,
        symbol=(args.symbol or base.symbol).upper(),
        frequency=args.frequency or base.frequency,
        base_contribution=_decimal_or_default(
            args.contribution_amount,
            base.base_contribution,
        ),
        day_of_month=(
            args.day_of_month
            if args.day_of_month is not None
            else base.day_of_month
        ),
        biweekly_anchor_date=(
            date.fromisoformat(args.biweekly_anchor_date)
            if args.biweekly_anchor_date
            else base.biweekly_anchor_date
        ),
        sizing_mode=args.sizing_mode or base.sizing_mode,
        drawdown_scale_factor=_decimal_or_default(
            args.drawdown_scale_factor,
            base.drawdown_scale_factor,
        ),
        drawdown_lookback_days=(
            args.drawdown_lookback_days
            if args.drawdown_lookback_days is not None
            else base.drawdown_lookback_days
        ),
        max_contribution_multiplier=_optional_decimal_override(
            args.max_contribution_multiplier,
            base.max_contribution_multiplier,
        ),
        max_contribution_per_purchase=_optional_decimal_override(
            args.max_contribution_per_purchase,
            base.max_contribution_per_purchase,
        ),
        max_annual_contribution=_optional_decimal_override(
            args.max_annual_contribution,
            base.max_annual_contribution,
        ),
        allow_fractional_shares=(
            False if args.whole_shares else base.allow_fractional_shares
        ),
    )


def _decimal_or_default(value: object, default: Decimal) -> Decimal:
    return default if value is None else Decimal(str(value))


def _optional_decimal_override(
    value: object,
    default: Decimal | None,
) -> Decimal | None:
    if value is None:
        return default
    if str(value).strip().lower() in {"off", "none", "null"}:
        return None
    return Decimal(str(value))
