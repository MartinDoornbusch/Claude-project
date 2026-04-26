"""Trade manager — schakelt transparant tussen paper en live trading."""

from __future__ import annotations

import logging
import os

from python_bitvavo_api.bitvavo import Bitvavo

import src.paper_trader as paper
import src.live_trader as live
from src.database import get_position

logger = logging.getLogger(__name__)

def mode() -> str:
    return "LIVE" if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true" else "PAPER"


def check_sl_tp(client: Bitvavo, market: str, current_price: float) -> bool:
    """
    Controleert trailing stop, breakeven trigger, stop-loss en take-profit.
    Verkoopt automatisch en geeft True terug als er verkocht is.
    """
    from src.database import get_position_meta, update_position_peak, set_breakeven_activated, clear_position_meta

    _sl_raw  = os.getenv("STOP_LOSS_PCT",        "").strip()
    _tp_raw  = os.getenv("TAKE_PROFIT_PCT",      "").strip()
    _be_raw  = os.getenv("BREAKEVEN_TRIGGER_PCT","").strip()
    trailing_enabled = os.getenv("TRAILING_STOP_ENABLED", "false").lower() == "true"
    trailing_pct     = float(os.getenv("TRAILING_STOP_PCT", "2.0"))

    stop_loss_pct:    float | None = float(_sl_raw)  if _sl_raw  else None
    take_profit_pct:  float | None = float(_tp_raw)  if _tp_raw  else None
    breakeven_trigger: float | None = float(_be_raw) if _be_raw  else None

    nothing_to_check = (
        stop_loss_pct is None and take_profit_pct is None
        and not trailing_enabled and breakeven_trigger is None
    )
    if nothing_to_check:
        return False

    pos = get_position(market)
    if pos["amount"] <= 0 or pos["avg_price"] <= 0:
        return False

    avg_price = pos["avg_price"]
    chg_pct   = (current_price - avg_price) / avg_price * 100
    meta      = get_position_meta(market)

    # ── Trailing stop: update piek ────────────────────────────────────────────
    if trailing_enabled:
        peak = meta["peak_price"]
        if peak <= 0 or current_price > peak:
            update_position_peak(market, current_price)
            peak = current_price
            meta = get_position_meta(market)

        trailing_stop_price = peak * (1 - trailing_pct / 100)
        if current_price <= trailing_stop_price:
            from src.notifier import notify_sl_tp
            reason = f"Trailing stop ({chg_pct:.1f}% | piek €{peak:.4f})"
            logger.warning("[%s] TRAILING STOP geraakt: piek €%.4f → stop €%.4f",
                           market, peak, trailing_stop_price)
            execute_sell(client, market, current_price, reason=reason)
            notify_sl_tp(market, "Trailing stop", chg_pct, current_price)
            clear_position_meta(market)
            return True

    # ── Breakeven trigger ─────────────────────────────────────────────────────
    if breakeven_trigger is not None and chg_pct >= breakeven_trigger and not meta["breakeven_set"]:
        set_breakeven_activated(market)
        logger.info("[%s] Breakeven geactiveerd op %.1f%% winst — stop naar instapprijs", market, chg_pct)
        meta = get_position_meta(market)

    # Effectieve stop: als breakeven actief → maximaal 0% (instapprijs)
    effective_sl = stop_loss_pct
    if meta["breakeven_set"]:
        effective_sl = max(stop_loss_pct, 0.0) if stop_loss_pct is not None else 0.0

    # ── Statische / breakeven stop ────────────────────────────────────────────
    if effective_sl is not None and chg_pct <= effective_sl:
        from src.notifier import notify_sl_tp
        sl_label = "Breakeven stop" if (meta["breakeven_set"] and effective_sl == 0.0) else "Stop-loss"
        reason   = f"{sl_label} ({chg_pct:.1f}%)"
        logger.warning("[%s] %s geraakt: %.1f%%", market, sl_label, chg_pct)
        execute_sell(client, market, current_price, reason=reason)
        notify_sl_tp(market, sl_label, chg_pct, current_price)
        clear_position_meta(market)
        return True

    # ── Take-profit ───────────────────────────────────────────────────────────
    if take_profit_pct is not None and chg_pct >= take_profit_pct:
        from src.notifier import notify_sl_tp
        reason = f"Take-profit ({chg_pct:.1f}%)"
        logger.info("[%s] TAKE-PROFIT geraakt: %.1f%%", market, chg_pct)
        execute_sell(client, market, current_price, reason=reason)
        notify_sl_tp(market, "Take-profit", chg_pct, current_price)
        clear_position_meta(market)
        return True

    return False


def execute_buy(
    client: Bitvavo, market: str, price: float, reason: str = "",
    fraction: float | None = None, iceberg_chunks: int = 1,
) -> dict | None:
    from src.notifier import notify_trade
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        logger.info("[%s] Mode: LIVE — BUY uitvoeren", market)
        result = live.buy(client, market, price, reason, iceberg_chunks=iceberg_chunks)
    else:
        logger.info("[%s] Mode: PAPER — BUY simuleren", market)
        result = paper.buy(market, price, reason, fraction=fraction, iceberg_chunks=iceberg_chunks)
    if result:
        notify_trade(market, "BUY", price, reason)
        from src.database import update_position_peak
        update_position_peak(market, price)
    return result


def execute_sell(client: Bitvavo, market: str, price: float, reason: str = "") -> dict | None:
    from src.notifier import notify_trade
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        logger.info("[%s] Mode: LIVE — SELL uitvoeren", market)
        result = live.sell(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — SELL simuleren", market)
        result = paper.sell(market, price, reason)
    if result:
        notify_trade(market, "SELL", price, reason)
    return result
