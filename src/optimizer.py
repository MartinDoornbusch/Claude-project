"""Strategie-optimalisatie — grid-search over SMA/RSI parameters via de backtester."""

from __future__ import annotations

import logging
from itertools import product

from src.backtester import run_backtest

logger = logging.getLogger(__name__)

SMA_SHORT_OPTIONS = [10, 15, 20, 25, 30]
SMA_LONG_OPTIONS  = [40, 50, 60, 80, 100]
RSI_BUY_OPTIONS   = [20, 25, 30]
RSI_SELL_OPTIONS  = [70, 75, 80]


def run_optimization(
    df,
    market: str,
    interval: str,
    capital: float = 1000.0,
) -> list[dict]:
    """
    Test alle combinaties van SMA- en RSI-parameters via de backtester.
    Retourneert resultaten gesorteerd op Sharpe ratio (beste eerst).
    """
    combos = [
        (s, l, rb, rs)
        for s, l, rb, rs in product(
            SMA_SHORT_OPTIONS, SMA_LONG_OPTIONS, RSI_BUY_OPTIONS, RSI_SELL_OPTIONS
        )
        if s < l
    ]

    logger.info("Optimalisatie gestart: %d combinaties voor %s", len(combos), market)
    results = []

    for sma_s, sma_l, rsi_b, rsi_s in combos:
        try:
            r = run_backtest(
                df, market, interval,
                initial_capital=capital,
                sma_short=sma_s,
                sma_long=sma_l,
                rsi_buy=rsi_b,
                rsi_sell=rsi_s,
            )
            results.append({
                "sma_short":   sma_s,
                "sma_long":    sma_l,
                "rsi_buy":     rsi_b,
                "rsi_sell":    rsi_s,
                "return_pct":  r.total_return_pct,
                "sharpe":      r.sharpe_ratio,
                "max_dd":      r.max_drawdown_pct,
                "win_rate":    r.win_rate_pct,
                "num_trades":  r.num_trades,
                "final":       r.final_capital,
            })
        except Exception as exc:
            logger.debug("Combo SMA(%d,%d) RSI(%d,%d) mislukt: %s", sma_s, sma_l, rsi_b, rsi_s, exc)

    results.sort(key=lambda x: (x["sharpe"] or -99, x["return_pct"]), reverse=True)
    logger.info("Optimalisatie klaar: %d resultaten", len(results))
    return results
