"""MA-crossover strategie — geeft BUY / SELL / HOLD terug."""

from __future__ import annotations

import logging
import os

import pandas as pd

from src.candles import add_indicators, latest_signals, get_htf_trend, get_higher_timeframe

logger = logging.getLogger(__name__)

def evaluate(market: str, interval: str, df: pd.DataFrame, client=None) -> str | None:
    """
    Evalueer de MA-crossover strategie op de gegeven candle DataFrame.

    Logica:
    - SMA20 kruist omhoog door SMA50  → BUY  (golden cross)
    - SMA20 kruist omlaag door SMA50  → SELL (death cross)
    - RSI > 75                         → SELL (overbought)
    - RSI < 25                         → BUY  (oversold dip)
    - Multi-timeframe filter (optioneel): BUY alleen bij UP-trend op HTF;
      SELL alleen bij DOWN-trend op HTF; NEUTRAL laat signal door.

    Retourneert: "BUY" | "SELL" | "HOLD"
    """
    if len(df) < 51:
        logger.warning("[%s] Te weinig candles voor strategie (%d)", market, len(df))
        return None

    df = add_indicators(df)
    signals = latest_signals(df)

    signal = "HOLD"
    reason = ""

    ma_cross = signals.get("ma_cross")
    rsi = signals.get("rsi_14")

    if ma_cross == "golden_cross":
        signal = "BUY"
        reason = "Golden cross (SMA20 > SMA50)"
    elif ma_cross == "death_cross":
        signal = "SELL"
        reason = "Death cross (SMA20 < SMA50)"
    elif rsi is not None and rsi > 75:
        signal = "SELL"
        reason = f"RSI overbought ({rsi:.1f})"
    elif rsi is not None and rsi < 25:
        signal = "BUY"
        reason = f"RSI oversold ({rsi:.1f})"

    # ── Multi-timeframe filter ──
    mtf_enabled = os.getenv("MTF_ENABLED", "true").lower() == "true"
    if mtf_enabled and client is not None and signal in ("BUY", "SELL"):
        htf = get_higher_timeframe(interval)
        if htf != interval:
            htf_trend = get_htf_trend(client, market, interval)
            if signal == "BUY" and htf_trend == "DOWN":
                logger.info(
                    "[%s] BUY gefilterd door MTF — %s trend is DOWN", market, htf
                )
                signal = "HOLD"
                reason = ""
            elif signal == "SELL" and htf_trend == "UP":
                logger.info(
                    "[%s] SELL gefilterd door MTF — %s trend is UP", market, htf
                )
                signal = "HOLD"
                reason = ""

    logger.info(
        "[%s] Signaal: %s | Prijs: €%.4f | RSI: %s | Reden: %s",
        market,
        signal,
        signals.get("close", 0),
        f"{rsi:.1f}" if rsi else "n/a",
        reason or "geen",
    )

    return signal
