"""Marktcorrelatie — voorkomt dubbele blootstelling aan sterk gecorreleerde markten."""

from __future__ import annotations

import logging
import os
import time as _time

import pandas as pd

from src.env_utils import env_float

logger = logging.getLogger(__name__)

CORR_LOOKBACK = 30  # dagelijkse candles
_CACHE_TTL = 3600   # dagelijkse candles veranderen maximaal 1×/dag

_price_cache: dict[str, tuple[pd.Series, float]] = {}  # market → (series, timestamp)


def _get_cached_prices(client, markets: list[str]) -> dict[str, pd.Series]:
    """Haalt dagelijkse sluitingskoersen op met 1-uurs cache."""
    from src.candles import get_candles
    now = _time.monotonic()
    prices: dict[str, pd.Series] = {}
    for m in markets:
        cached = _price_cache.get(m)
        if cached and (now - cached[1]) < _CACHE_TTL:
            prices[m] = cached[0]
        else:
            try:
                df = get_candles(client, m, "1d", limit=CORR_LOOKBACK + 5)
                series = df.set_index("timestamp")["close"]
                _price_cache[m] = (series, now)
                prices[m] = series
            except Exception:
                pass
    return prices


def get_correlated_markets(
    client,
    market: str,
    all_markets: list[str],
    threshold: float | None = None,
) -> list[str]:
    """
    Retourneert markten die sterk gecorreleerd zijn met `market` (>= threshold).
    Gebruikt 30 dagelijkse sluitingskoersen voor berekening (gecached per uur).
    """
    if threshold is None:
        threshold = env_float("CORR_THRESHOLD", 0.8)

    if len(all_markets) < 2:
        return []

    prices = _get_cached_prices(client, all_markets)

    if market not in prices:
        return []

    target_returns = prices[market].pct_change().dropna()
    correlated = []

    for m, series in prices.items():
        if m == market:
            continue
        try:
            other_returns = series.pct_change().dropna()
            combined = pd.concat([target_returns, other_returns], axis=1).dropna()
            if len(combined) < 10:
                continue
            corr = combined.iloc[:, 0].corr(combined.iloc[:, 1])
            if corr >= threshold:
                correlated.append(m)
                logger.debug("[%s↔%s] correlatie: %.2f (boven %.2f)", market, m, corr, threshold)
        except Exception:
            pass

    return correlated


def has_correlated_position(
    client,
    market: str,
    all_markets: list[str],
    threshold: float | None = None,
) -> tuple[bool, str]:
    """
    Controleert of er al een open positie is in een gecorreleerde markt.
    Retourneert (True, markt_naam) als dat zo is, anders (False, "").
    """
    if threshold is None:
        threshold = env_float("CORR_THRESHOLD", 0.8)

    from src.database import get_position

    correlated = get_correlated_markets(client, market, all_markets, threshold)
    for m in correlated:
        pos = get_position(m)
        if pos.get("amount", 0) > 0:
            logger.info(
                "[%s] BUY overgeslagen — gecorreleerde positie open in %s", market, m
            )
            return True, m
    return False, ""
