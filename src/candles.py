"""Candle data ophalen en basis technische indicatoren berekenen."""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from python_bitvavo_api.bitvavo import Bitvavo

# Bitvavo interval opties: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d
DEFAULT_INTERVAL = "1h"
DEFAULT_LIMIT = 200


def get_candles(client: Bitvavo, market: str, interval: str = DEFAULT_INTERVAL, limit: int = DEFAULT_LIMIT) -> pd.DataFrame:
    """
    Haal OHLCV candles op en retourneer als DataFrame.
    Kolommen: timestamp, open, high, low, close, volume
    """
    result = client.candles(market, interval, {"limit": limit})

    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Bitvavo fout: {result['error']}")

    df = pd.DataFrame(result, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Voeg basis technische indicatoren toe aan een candle DataFrame.
    - SMA 20 en SMA 50  (trend)
    - RSI 14            (momentum / overbought-oversold)
    - MACD              (trendkracht)
    - Bollinger Bands   (volatiliteit)
    """
    df = df.copy()

    df["sma_20"] = ta.sma(df["close"], length=20)
    df["sma_50"] = ta.sma(df["close"], length=50)

    df["rsi_14"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"])
    if macd is not None:
        df["macd"] = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
        df["macd_hist"] = macd["MACDh_12_26_9"]

    bbands = ta.bbands(df["close"], length=20)
    if bbands is not None:
        df["bb_lower"] = bbands["BBL_20_2.0"]
        df["bb_mid"] = bbands["BBM_20_2.0"]
        df["bb_upper"] = bbands["BBU_20_2.0"]

    return df


def latest_signals(df: pd.DataFrame) -> dict:
    """Geef de meest recente indicator-waarden terug als dict."""
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    ma_cross = None
    if pd.notna(last.get("sma_20")) and pd.notna(last.get("sma_50")):
        if prev["sma_20"] < prev["sma_50"] and last["sma_20"] >= last["sma_50"]:
            ma_cross = "golden_cross"
        elif prev["sma_20"] > prev["sma_50"] and last["sma_20"] <= last["sma_50"]:
            ma_cross = "death_cross"

    return {
        "close": last["close"],
        "sma_20": last.get("sma_20"),
        "sma_50": last.get("sma_50"),
        "rsi_14": last.get("rsi_14"),
        "macd": last.get("macd"),
        "macd_signal": last.get("macd_signal"),
        "bb_lower": last.get("bb_lower"),
        "bb_upper": last.get("bb_upper"),
        "ma_cross": ma_cross,
    }
