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
    Controleert stop-loss en take-profit voor een open positie.
    Voert automatisch een SELL uit als de drempel bereikt is.
    Geeft True terug als er verkocht is.
    """
    _sl_raw = os.getenv("STOP_LOSS_PCT", "").strip()
    _tp_raw = os.getenv("TAKE_PROFIT_PCT", "").strip()
    stop_loss_pct:   float | None = float(_sl_raw) if _sl_raw else None
    take_profit_pct: float | None = float(_tp_raw) if _tp_raw else None

    if stop_loss_pct is None and take_profit_pct is None:
        return False

    pos = get_position(market)
    if pos["amount"] <= 0 or pos["avg_price"] <= 0:
        return False

    chg_pct = (current_price - pos["avg_price"]) / pos["avg_price"] * 100

    if stop_loss_pct is not None and chg_pct <= stop_loss_pct:
        from src.notifier import notify_sl_tp
        reason = f"Stop-loss ({chg_pct:.1f}%)"
        logger.warning("[%s] STOP-LOSS geraakt: %.1f%% — verkopen", market, chg_pct)
        execute_sell(client, market, current_price, reason=reason)
        notify_sl_tp(market, "Stop-loss", chg_pct, current_price)
        return True

    if take_profit_pct is not None and chg_pct >= take_profit_pct:
        from src.notifier import notify_sl_tp
        reason = f"Take-profit ({chg_pct:.1f}%)"
        logger.info("[%s] TAKE-PROFIT geraakt: %.1f%% — verkopen", market, chg_pct)
        execute_sell(client, market, current_price, reason=reason)
        notify_sl_tp(market, "Take-profit", chg_pct, current_price)
        return True

    return False


def execute_buy(
    client: Bitvavo, market: str, price: float, reason: str = "", fraction: float | None = None
) -> dict | None:
    from src.notifier import notify_trade
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        logger.info("[%s] Mode: LIVE — BUY uitvoeren", market)
        result = live.buy(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — BUY simuleren", market)
        result = paper.buy(market, price, reason, fraction=fraction)
    if result:
        notify_trade(market, "BUY", price, reason)
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
