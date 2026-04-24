"""Backtester — simuleert de MA-crossover + RSI strategie op historische candle-data."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.candles import add_indicators_custom

FEE_RATE = 0.0025  # Bitvavo taker fee


@dataclass
class Trade:
    buy_ts: str
    buy_price: float
    amount: float
    reason_buy: str = ""
    sell_ts: Optional[str] = None
    sell_price: Optional[float] = None
    pnl_eur: float = 0.0
    pnl_pct: float = 0.0
    reason_sell: str = ""


@dataclass
class BacktestResult:
    market: str
    interval: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    num_trades: int
    num_wins: int
    win_rate_pct: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]
    best_trade_pct: float
    worst_trade_pct: float
    avg_trade_pct: float
    candles_tested: int
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[str] = field(default_factory=list)


def _signal(prev: dict, curr: dict, rsi_buy: float = 25.0, rsi_sell: float = 75.0) -> tuple[str, str]:
    """Retourneert (signal, reason) op basis van twee opeenvolgende candle-rijen."""
    sp = prev.get("sma_short")
    lp = prev.get("sma_long")
    sc = curr.get("sma_short")
    lc = curr.get("sma_long")
    rsi = curr.get("rsi_14")

    if None in (sp, lp, sc, lc):
        return "HOLD", ""
    if sp < lp and sc > lc:
        return "BUY",  "Golden cross"
    if sp > lp and sc < lc:
        return "SELL", "Death cross"
    if rsi is not None and rsi > rsi_sell:
        return "SELL", f"RSI overbought ({rsi:.1f})"
    if rsi is not None and rsi < rsi_buy:
        return "BUY",  f"RSI oversold ({rsi:.1f})"
    return "HOLD", ""


def run_backtest(
    df: pd.DataFrame,
    market: str,
    interval: str,
    initial_capital: float = 1000.0,
    trade_fraction: float = 0.95,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    sma_short: int = 20,
    sma_long: int = 50,
    rsi_window: int = 14,
    rsi_buy: float = 25.0,
    rsi_sell: float = 75.0,
) -> BacktestResult:
    """
    Simuleert de strategie op historische data.

    Args:
        df:               Candle DataFrame (ruwe data, indicatoren worden toegevoegd).
        market:           Handelspaar, bijv. 'BTC-EUR'.
        interval:         Candle interval, bijv. '1h'.
        initial_capital:  Startkapitaal in EUR.
        trade_fraction:   Deel van cash per BUY (0.95 = 95%).
        stop_loss_pct:    Optionele stop-loss als negatief percentage, bijv. -5.0.
        take_profit_pct:  Optionele take-profit als positief percentage, bijv. 10.0.
        sma_short:        Korte SMA venster (default 20).
        sma_long:         Lange SMA venster (default 50).
        rsi_window:       RSI venster (default 14).
        rsi_buy:          RSI oversold drempel voor BUY (default 25).
        rsi_sell:         RSI overbought drempel voor SELL (default 75).
    """
    df = add_indicators_custom(
        df.copy(), sma_short=sma_short, sma_long=sma_long, rsi_window=rsi_window
    ).dropna(subset=["sma_short", "sma_long"]).reset_index(drop=True)

    if len(df) < 2:
        raise ValueError(f"Te weinig candles voor backtesting: {len(df)} (min. 52 nodig)")

    cash = initial_capital
    pos_amount = 0.0
    pos_price = 0.0
    open_trade: Optional[Trade] = None
    trades: list[Trade] = []
    equity_curve: list[float] = []
    timestamps: list[str] = []
    rows = df.to_dict("records")

    for i in range(1, len(rows)):
        prev  = rows[i - 1]
        curr  = rows[i]
        price = float(curr["close"])
        ts    = str(curr.get("timestamp", i))

        equity_curve.append(cash + pos_amount * price)
        timestamps.append(ts[:16] if len(ts) > 16 else ts)

        # ── Stop-loss / take-profit check ──
        sell_reason = ""
        if pos_amount > 0 and pos_price > 0:
            chg_pct = (price - pos_price) / pos_price * 100
            if stop_loss_pct is not None and chg_pct <= stop_loss_pct:
                sell_reason = f"Stop-loss ({chg_pct:.1f}%)"
            elif take_profit_pct is not None and chg_pct >= take_profit_pct:
                sell_reason = f"Take-profit ({chg_pct:.1f}%)"

        # ── Strategy signal ──
        sig, sig_reason = _signal(prev, curr, rsi_buy=rsi_buy, rsi_sell=rsi_sell)
        if not sell_reason and sig == "SELL" and pos_amount > 0:
            sell_reason = sig_reason

        # ── Execute SELL ──
        if sell_reason and pos_amount > 0:
            gross = pos_amount * price
            net   = gross * (1 - FEE_RATE)
            pnl   = net - (pos_amount * pos_price / (1 - FEE_RATE))
            cash += net
            if open_trade:
                open_trade.sell_ts    = ts
                open_trade.sell_price = price
                open_trade.pnl_eur    = round(pnl, 4)
                open_trade.pnl_pct    = round((price - pos_price) / pos_price * 100, 3)
                open_trade.reason_sell = sell_reason
                trades.append(open_trade)
                open_trade = None
            pos_amount = 0.0
            pos_price  = 0.0

        # ── Execute BUY ──
        elif sig == "BUY" and pos_amount == 0 and cash > 10:
            spend      = cash * trade_fraction
            amount     = spend * (1 - FEE_RATE) / price
            cash      -= spend
            pos_amount = amount
            pos_price  = price
            open_trade = Trade(buy_ts=ts, buy_price=price, amount=amount, reason_buy=sig_reason)

    # ── Sluit open positie aan het einde ──
    if pos_amount > 0:
        last_price = float(rows[-1]["close"])
        gross = pos_amount * last_price
        net   = gross * (1 - FEE_RATE)
        pnl   = net - (pos_amount * pos_price / (1 - FEE_RATE))
        cash += net
        if open_trade:
            open_trade.sell_ts     = str(rows[-1].get("timestamp", "—"))
            open_trade.sell_price  = last_price
            open_trade.pnl_eur     = round(pnl, 4)
            open_trade.pnl_pct     = round((last_price - pos_price) / pos_price * 100, 3)
            open_trade.reason_sell = "Einde testperiode"
            trades.append(open_trade)

    # ── Metrics ──
    final   = round(cash, 4)
    ret_pct = round((final - initial_capital) / initial_capital * 100, 2)
    wins    = [t for t in trades if t.pnl_eur > 0]
    win_rt  = round(len(wins) / len(trades) * 100, 1) if trades else 0.0

    # Max drawdown
    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100
        max_dd = min(max_dd, dd)

    # Sharpe ratio (vereenvoudigd, geannualiseerd)
    sharpe = None
    if len(equity_curve) > 10:
        rets = pd.Series(equity_curve).pct_change().dropna()
        std  = rets.std()
        if std > 0:
            periods = {"1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520,
                       "1h": 8760, "2h": 4380, "4h": 2190, "6h": 1460, "1d": 365}
            ann = math.sqrt(periods.get(interval, 8760))
            sharpe = round(rets.mean() / std * ann, 2)

    pcts = [t.pnl_pct for t in trades]

    return BacktestResult(
        market=market,
        interval=interval,
        initial_capital=initial_capital,
        final_capital=final,
        total_return_pct=ret_pct,
        num_trades=len(trades),
        num_wins=len(wins),
        win_rate_pct=win_rt,
        max_drawdown_pct=round(max_dd, 2),
        sharpe_ratio=sharpe,
        best_trade_pct=round(max(pcts), 2) if pcts else 0.0,
        worst_trade_pct=round(min(pcts), 2) if pcts else 0.0,
        avg_trade_pct=round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
        candles_tested=len(rows),
        trades=trades,
        equity_curve=equity_curve,
        timestamps=timestamps,
    )
