"""Trade manager — schakelt transparant tussen paper en live trading."""

from __future__ import annotations

import logging
import os

from python_bitvavo_api.bitvavo import Bitvavo

import src.paper_trader as paper
import src.live_trader as live
from src.database import get_position, set_house_money_activated
from src.env_utils import env_float, env_float_opt

logger = logging.getLogger(__name__)

def mode() -> str:
    return "LIVE" if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true" else "PAPER"


def check_sl_tp(client: Bitvavo, market: str, current_price: float) -> bool:
    """
    Controleert trailing stop, breakeven trigger, stop-loss en take-profit.
    Verkoopt automatisch en geeft True terug als er verkocht is.
    """
    from src.database import get_position_meta, update_position_peak, set_breakeven_activated, clear_position_meta

    trailing_enabled  = os.getenv("TRAILING_STOP_ENABLED", "false").lower() == "true"
    trailing_pct      = env_float("TRAILING_STOP_PCT", 2.0)
    stop_loss_pct     = env_float_opt("STOP_LOSS_PCT")
    take_profit_pct   = env_float_opt("TAKE_PROFIT_PCT")
    breakeven_trigger = env_float_opt("BREAKEVEN_TRIGGER_PCT")

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


def check_house_money(client: Bitvavo, market: str, current_price: float) -> bool:
    """Verkoopt bij X% winst precies genoeg om de initiële inleg terug te halen."""
    if os.getenv("HOUSE_MONEY_ENABLED", "false").lower() != "true":
        return False

    pos = get_position(market)
    if pos["amount"] <= 0 or pos["avg_price"] <= 0:
        return False

    from src.database import get_position_meta, set_house_money_activated
    meta = get_position_meta(market)
    if meta.get("house_money_activated"):
        return False

    trigger_pct = env_float("HOUSE_MONEY_TRIGGER_PCT", 10)
    chg_pct     = (current_price - pos["avg_price"]) / pos["avg_price"] * 100
    if chg_pct < trigger_pct:
        return False

    initial_eur = pos["amount"] * pos["avg_price"]
    qty_to_sell = min(initial_eur / current_price, pos["amount"] * 0.9999)

    reason = f"Huisgeld: inleg veiliggesteld bij {chg_pct:.1f}%"
    logger.info(
        "[%s] HUISGELD: verkoop %.6f om inleg €%.2f terug te halen bij %.1f%% winst",
        market, qty_to_sell, initial_eur, chg_pct,
    )

    from src.notifier import notify_trade
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        result = live.partial_sell(client, market, qty_to_sell, current_price, reason)
    else:
        result = paper.partial_sell(market, qty_to_sell, current_price, reason)

    if result:
        set_house_money_activated(market)
        notify_trade(market, "SELL", current_price, reason)
        return True
    return False


def execute_buy(
    client: Bitvavo, market: str, price: float, reason: str = "",
    fraction: float | None = None, iceberg_chunks: int = 1,
) -> dict | None:
    from src.notifier import notify_trade

    # Blacklist check
    blacklist = {m.strip().upper() for m in os.getenv("TRADING_BLACKLIST", "").split(",") if m.strip()}
    if market.upper() in blacklist:
        logger.info("[%s] BUY overgeslagen — markt staat op blacklist", market)
        return None

    # Huisgeld: alleen kopen als vorige trade winstgevend was
    if os.getenv("HOUSE_MONEY_ONLY_PROFIT", "false").lower() == "true":
        from src.database import get_last_trade_pnl
        last_pnl = get_last_trade_pnl(market)
        if last_pnl is not None and last_pnl <= 0:
            logger.info(
                "[%s] BUY overgeslagen — huisgeld: vorige trade verliesgevend (€%.2f)", market, last_pnl
            )
            return None

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
