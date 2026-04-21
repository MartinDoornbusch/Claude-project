"""MA-crossover strategie — geeft BUY / SELL / HOLD terug."""

from __future__ import annotations

import logging

import pandas as pd

from src.candles import add_indicators, latest_signals
from src.database import save_signal

logger = logging.getLogger(__name__)


def evaluate(market: str, interval: str, df: pd.DataFrame) -> str | None:
    """
    Evalueer de MA-crossover strategie op de gegeven candle DataFrame.

    Logica:
    - SMA20 kruist omhoog door SMA50  → BUY  (golden cross)
    - SMA20 kruist omlaag door SMA50  → SELL (death cross)
    - RSI > 75 terwijl long            → vroegtijdig SELL (overbought)
    - RSI < 25 terwijl geen positie    → extra BUY bevestiging (oversold dip)
    - Alle andere situaties            → HOLD (geen actie)

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

    save_signal(market, interval, signals, signal)

    logger.info(
        "[%s] Signaal: %s | Prijs: €%.4f | RSI: %s | Reden: %s",
        market,
        signal,
        signals.get("close", 0),
        f"{rsi:.1f}" if rsi else "n/a",
        reason or "geen",
    )

    return signal
