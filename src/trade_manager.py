"""Trade manager — schakelt transparant tussen paper en live trading."""

from __future__ import annotations

import logging
import os

from python_bitvavo_api.bitvavo import Bitvavo

import src.paper_trader as paper
import src.live_trader as live
from src.database import get_position

logger = logging.getLogger(__name__)

LIVE_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

# Stop-loss en take-profit drempelwaarden (leeg = uitgeschakeld)
_SL_RAW = os.getenv("STOP_LOSS_PCT", "").strip()
_TP_RAW = os.getenv("TAKE_PROFIT_PCT", "").strip()
STOP_LOSS_PCT:   float | None = float(_SL_RAW) if _SL_RAW else None
TAKE_PROFIT_PCT: float | None = float(_TP_RAW) if _TP_RAW else None


def mode() -> str:
    return "LIVE" if LIVE_ENABLED else "PAPER"


def check_sl_tp(client: Bitvavo, market: str, current_price: float) -> bool:
    """
    Controleert stop-loss en take-profit voor een open positie.
    Voert automatisch een SELL uit als de drempel bereikt is.
    Geeft True terug als er verkocht is.
    """
    if STOP_LOSS_PCT is None and TAKE_PROFIT_PCT is None:
        return False

    pos = get_position(market)
    if pos["amount"] <= 0 or pos["avg_price"] <= 0:
        return False

    chg_pct = (current_price - pos["avg_price"]) / pos["avg_price"] * 100

    if STOP_LOSS_PCT is not None and chg_pct <= STOP_LOSS_PCT:
        from src.notifier import notify_sl_tp
        reason = f"Stop-loss ({chg_pct:.1f}%)"
        logger.warning("[%s] STOP-LOSS geraakt: %.1f%% — verkopen", market, chg_pct)
        execute_sell(client, market, current_price, reason=reason)
        notify_sl_tp(market, "Stop-loss", chg_pct, current_price)
        return True

    if TAKE_PROFIT_PCT is not None and chg_pct >= TAKE_PROFIT_PCT:
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
    if LIVE_ENABLED:
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
    if LIVE_ENABLED:
        logger.info("[%s] Mode: LIVE — SELL uitvoeren", market)
        result = live.sell(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — SELL simuleren", market)
        result = paper.sell(market, price, reason)
    if result:
        notify_trade(market, "SELL", price, reason)
    return result
