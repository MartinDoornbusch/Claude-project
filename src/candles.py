"""Candle data ophalen en basis technische indicatoren berekenen."""

from __future__ import annotations

import pandas as pd
import ta.trend as ta_trend
import ta.momentum as ta_momentum
import ta.volatility as ta_volatility
from python_bitvavo_api.bitvavo import Bitvavo

DEFAULT_INTERVAL = "1h"
DEFAULT_LIMIT = 200


def get_candles(client: Bitvavo, market: str, interval: str = DEFAULT_INTERVAL, limit: int = DEFAULT_LIMIT) -> pd.DataFrame:
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
    df = df.copy()
    close = df["close"]

    df["sma_20"] = ta_trend.sma_indicator(close, window=20)
    df["sma_50"] = ta_trend.sma_indicator(close, window=50)
    df["rsi_14"] = ta_momentum.rsi(close, window=14)

    macd = ta_trend.MACD(close)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    bb = ta_volatility.BollingerBands(close, window=20, window_dev=2)
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_upper"] = bb.bollinger_hband()

    return df


_HTF_MAP: dict[str, str] = {
    "1m": "15m", "5m": "1h", "15m": "4h", "30m": "4h",
    "1h": "1d", "2h": "1d", "4h": "1d", "6h": "1d",
    "8h": "1d", "12h": "1d", "1d": "1d",
}


def get_higher_timeframe(interval: str) -> str:
    """Retourneert een hoger timeframe voor trendbevestiging."""
    return _HTF_MAP.get(interval, "1d")


def get_htf_trend(client: Bitvavo, market: str, interval: str) -> str:
    """
    Bepaalt de trendrichting op het hogere timeframe.
    Geeft 'UP', 'DOWN' of 'NEUTRAL' terug.
    """
    htf = get_higher_timeframe(interval)
    if htf == interval:
        return "NEUTRAL"
    try:
        df = get_candles(client, market, htf, limit=60)
        df = add_indicators(df)
        last = df.iloc[-1]
        sma20 = last.get("sma_20")
        sma50 = last.get("sma_50")
        if pd.notna(sma20) and pd.notna(sma50):
            if sma20 > sma50:
                return "UP"
            elif sma20 < sma50:
                return "DOWN"
    except Exception:
        pass
    return "NEUTRAL"


def latest_signals(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    ma_cross = None
    if pd.notna(last.get("sma_20")) and pd.notna(last.get("sma_50")):
        if prev["sma_20"] < prev["sma_50"] and last["sma_20"] >= last["sma_50"]:
            ma_cross = "golden_cross"
        elif prev["sma_20"] > prev["sma_50"] and last["sma_20"] <= last["sma_50"]:
            ma_cross = "death_cross"

    ts = last.get("timestamp")
    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

    return {
        "ts": ts_str,
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
