"""Candle data ophalen en basis technische indicatoren berekenen."""

from __future__ import annotations

import os

import pandas as pd

from src.env_utils import env_float, env_float_opt
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
    df["sma_200"] = ta_trend.sma_indicator(close, window=200)
    df["rsi_14"] = ta_momentum.rsi(close, window=14)

    macd = ta_trend.MACD(close)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    bb = ta_volatility.BollingerBands(close, window=20, window_dev=2)
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_upper"] = bb.bollinger_hband()

    atr = ta_volatility.AverageTrueRange(df["high"], df["low"], close, window=14)
    df["atr_14"] = atr.average_true_range()

    try:
        # VWAP (rollend 24-candle venster)
        typical = (df["high"] + df["low"] + close) / 3
        vol_sum = df["volume"].rolling(24).sum()
        df["vwap_24"] = (typical * df["volume"]).rolling(24).sum() / vol_sum.replace(0, float("nan"))
    except Exception:
        df["vwap_24"] = float("nan")

    try:
        # ADX — marktregime (> 25 trending, < 20 sideways)
        adx_ind = ta_trend.ADXIndicator(df["high"], df["low"], close, window=14)
        df["adx_14"] = adx_ind.adx()
    except Exception:
        df["adx_14"] = float("nan")

    return df


def add_indicators_custom(
    df: pd.DataFrame,
    sma_short: int = 20,
    sma_long: int = 50,
    rsi_window: int = 14,
) -> pd.DataFrame:
    """Berekent indicators met aangepaste venstergroottes (voor optimizer/backtester)."""
    df = df.copy()
    close = df["close"]
    df["sma_short"] = ta_trend.sma_indicator(close, window=sma_short)
    df["sma_long"]  = ta_trend.sma_indicator(close, window=sma_long)
    df["rsi_14"]    = ta_momentum.rsi(close, window=rsi_window)
    return df


def get_atr_fraction(df: pd.DataFrame, base_fraction: float = 0.95, target_vol_pct: float = 2.0) -> float:
    """
    Berekent een volatiliteits-aangepaste positiefractie op basis van ATR.
    Bij hoge volatiliteit kleiner inzetten, bij lage volatiliteit tot base_fraction.
    """
    last = df.iloc[-1]
    atr   = last.get("atr_14")
    price = float(last["close"])
    if atr is None or pd.isna(atr) or price <= 0:
        return base_fraction
    atr_pct = float(atr) / price * 100
    if atr_pct <= 0:
        return base_fraction
    fraction = base_fraction * (target_vol_pct / atr_pct)
    return round(min(max(fraction, 0.2), base_fraction), 3)


def get_risk_fraction(
    df: pd.DataFrame,
    portfolio_total: float,
    available_cash: float,
    risk_pct: float | None = None,
    sl_pct: float | None = None,
    entry_price: float | None = None,
) -> float:
    """
    PositionSize = (Equity × RiskPercent) / (EntryPrice - StopLossPrice)

    Retourneert een fractie van available_cash zodat de verliesbeperking
    precies risk_pct × portfolio_total EUR bedraagt.
    """
    if risk_pct is None:
        risk_pct = env_float("RISK_PER_TRADE_PCT", 1.0)
    if sl_pct is None:
        sl_pct = abs(env_float_opt("STOP_LOSS_PCT") or 5.0)
    if entry_price is None:
        entry_price = float(df.iloc[-1]["close"])

    if sl_pct <= 0 or portfolio_total <= 0 or available_cash <= 0 or entry_price <= 0:
        return env_float("PAPER_TRADE_FRACTION", 0.15)

    sl_price     = entry_price * (1 - sl_pct / 100)
    risk_eur     = portfolio_total * (risk_pct / 100)
    units        = risk_eur / (entry_price - sl_price)      # crypto units risked
    position_eur = units * entry_price
    fraction     = position_eur / available_cash
    return round(min(max(fraction, 0.05), 0.95), 3)


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

    volume     = float(last["volume"]) if pd.notna(last.get("volume")) else None
    vol_avg_20 = float(df["volume"].tail(20).mean()) if len(df) >= 20 else None
    atr_14     = float(last["atr_14"]) if pd.notna(last.get("atr_14")) else None
    macd_hist  = float(last["macd_hist"]) if pd.notna(last.get("macd_hist")) else None
    macd_hist_prev = float(df.iloc[-2]["macd_hist"]) if len(df) > 1 and pd.notna(df.iloc[-2].get("macd_hist")) else None

    # 24-candle ATR average for Route B dynamic threshold
    _atr_series = df["atr_14"].tail(24).dropna() if "atr_14" in df.columns else pd.Series([], dtype=float)
    avg_atr_24h = float(_atr_series.mean()) if len(_atr_series) >= 12 else None

    # VWAP & ADX
    vwap_24 = float(last["vwap_24"]) if pd.notna(last.get("vwap_24")) else None
    adx_14  = float(last["adx_14"])  if pd.notna(last.get("adx_14"))  else None

    # RSI divergentie — vergelijk laatste candle met 10 candles terug
    rsi_divergence: str | None = None
    rsi_14 = last.get("rsi_14")
    if len(df) >= 11 and pd.notna(rsi_14):
        lookback = df.iloc[-11]
        price_chg = float(last["close"]) - float(lookback["close"])
        rsi_chg   = float(rsi_14) - float(lookback["rsi_14"]) if pd.notna(lookback.get("rsi_14")) else 0.0
        if price_chg < -0.001 * float(last["close"]) and rsi_chg > 3:
            rsi_divergence = "bullish"   # prijs daalt maar RSI stijgt → koop-signaal
        elif price_chg > 0.001 * float(last["close"]) and rsi_chg < -3:
            rsi_divergence = "bearish"   # prijs stijgt maar RSI daalt → verkoop-signaal

    # Support & Resistance — swing extremen over laatste 50 candles
    lookback_sr = df.tail(50)
    support    = float(lookback_sr["low"].min())   if len(lookback_sr) >= 10 else None
    resistance = float(lookback_sr["high"].max())  if len(lookback_sr) >= 10 else None

    return {
        "ts": ts_str,
        "close": last["close"],
        "sma_20": last.get("sma_20"),
        "sma_50": last.get("sma_50"),
        "rsi_14": rsi_14,
        "macd": last.get("macd"),
        "macd_signal": last.get("macd_signal"),
        "macd_hist": macd_hist,
        "macd_hist_prev": macd_hist_prev,
        "bb_lower": last.get("bb_lower"),
        "bb_upper": last.get("bb_upper"),
        "ma_cross": ma_cross,
        "volume": volume,
        "volume_avg_20": vol_avg_20,
        "atr_14": atr_14,
        "avg_atr_24h": avg_atr_24h,
        "sma_200": float(last["sma_200"]) if pd.notna(last.get("sma_200")) else None,
        "vwap_24": vwap_24,
        "adx_14": adx_14,
        "rsi_divergence": rsi_divergence,
        "support": support,
        "resistance": resistance,
    }
