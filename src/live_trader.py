"""Live trader — echte orders plaatsen via Bitvavo met veiligheidslimieten."""

from __future__ import annotations

import logging
import os
import time

from python_bitvavo_api.bitvavo import Bitvavo

from src.database import (
    save_live_trade, update_live_trade,
    get_live_trades, add_daily_pnl,
)

logger = logging.getLogger(__name__)

FEE_RATE = 0.0025  # Bitvavo taker fee


def _guard_checks(client: Bitvavo, market: str, spend_eur: float) -> str | None:
    """
    Controleer alle veiligheidslimieten vóór een order.
    Retourneert een foutmelding als een limiet overschreden wordt, anders None.
    """
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true":
        return "LIVE_TRADING_ENABLED is niet true in .env"

    max_trade_eur      = float(os.getenv("MAX_TRADE_EUR", "25"))
    max_exposure_eur   = float(os.getenv("MAX_EXPOSURE_EUR", "100"))
    daily_loss_pct     = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "2.0"))
    portfolio_basis    = float(os.getenv("PAPER_STARTING_CAPITAL", "1000"))
    daily_loss_limit   = portfolio_basis * daily_loss_pct / 100

    if spend_eur > max_trade_eur:
        return f"Order (€{spend_eur:.2f}) overschrijdt MAX_TRADE_EUR (€{max_trade_eur})"

    from src.database import get_total_daily_loss
    daily_loss = get_total_daily_loss()
    if daily_loss < 0 and abs(daily_loss) >= daily_loss_limit:
        return (f"Daglimiet bereikt: verlies vandaag €{abs(daily_loss):.2f} "
                f">= {daily_loss_pct}% van €{portfolio_basis:.0f} (€{daily_loss_limit:.2f})")

    open_trades = get_live_trades(market, limit=100)
    open_exposure = sum(
        t["eur_total"] for t in open_trades
        if t["side"] == "BUY" and t["status"] == "filled"
    )
    open_sell = sum(
        t["eur_total"] for t in open_trades
        if t["side"] == "SELL" and t["status"] == "filled"
    )
    net_exposure = open_exposure - open_sell
    if net_exposure + spend_eur > max_exposure_eur:
        return (f"Blootstelling (€{net_exposure:.2f} + €{spend_eur:.2f}) "
                f"overschrijdt MAX_EXPOSURE_EUR (€{max_exposure_eur})")

    return None


def _poll_order(client: Bitvavo, market: str, order_id: str, timeout: int = 30) -> dict | None:
    """Wacht tot een order gevuld is (max timeout seconden)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = client.getOrder(market, order_id)
        if isinstance(result, dict) and result.get("status") == "filled":
            return result
        if isinstance(result, dict) and result.get("status") in ("cancelled", "expired"):
            return result
        time.sleep(2)
    return None


def buy(client: Bitvavo, market: str, current_price: float, reason: str = "") -> dict | None:
    """
    Plaats een echte markt-koop order op Bitvavo.
    Gebruikt MAX_TRADE_EUR als orderbedrag.
    """
    spend_eur = float(os.getenv("MAX_TRADE_EUR", "25"))

    block = _guard_checks(client, market, spend_eur)
    if block:
        logger.warning("[%s] LIVE BUY geblokkeerd: %s", market, block)
        return None

    logger.info("[%s] LIVE BUY plaatsen — €%.2f | reden: %s", market, spend_eur, reason)

    trade_id = save_live_trade(market, "BUY", None, current_price, None, spend_eur, "pending", reason)

    result = client.placeOrder(market, "buy", "market", {"amountQuote": str(spend_eur)})

    if isinstance(result, dict) and "error" in result:
        update_live_trade(trade_id, current_price, 0, spend_eur, "error")
        logger.error("[%s] LIVE BUY mislukt: %s", market, result["error"])
        return None

    order_id = result.get("orderId", "")
    filled = _poll_order(client, market, order_id)

    if filled and filled.get("status") == "filled":
        filled_price = float(filled.get("price") or current_price)
        filled_amount = float(filled.get("filledAmount", 0))
        eur_total = float(filled.get("filledAmountQuote", spend_eur))
        update_live_trade(trade_id, filled_price, filled_amount, eur_total, "filled")
        logger.info(
            "[%s] LIVE BUY gevuld — prijs: €%.4f | bedrag: %.6f | totaal: €%.2f",
            market, filled_price, filled_amount, eur_total,
        )
        return {"side": "BUY", "order_id": order_id, "price": filled_price,
                "amount": filled_amount, "eur": eur_total}
    else:
        update_live_trade(trade_id, current_price, 0, spend_eur, "timeout")
        logger.warning("[%s] LIVE BUY order niet gevuld binnen timeout", market)
        return None


def sell(client: Bitvavo, market: str, current_price: float, reason: str = "") -> dict | None:
    """
    Sluit de open positie via een echte markt-verkoop order.
    Haalt de te verkopen hoeveelheid op uit de Bitvavo balance.
    """
    block = _guard_checks(client, market, 0)
    if block and "LIVE_TRADING_ENABLED" not in block:
        logger.warning("[%s] LIVE SELL geblokkeerd: %s", market, block)
        return None

    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true":
        logger.warning("[%s] LIVE SELL geblokkeerd: LIVE_TRADING_ENABLED is niet true", market)
        return None

    symbol = market.split("-")[0]
    balances = client.balance({"symbol": symbol})
    if isinstance(balances, dict) and "error" in balances:
        logger.error("[%s] Balance ophalen mislukt: %s", market, balances["error"])
        return None

    available = next(
        (float(b["available"]) for b in balances if b["symbol"] == symbol), 0.0
    )

    if available <= 0:
        logger.info("[%s] LIVE SELL overgeslagen — geen balance gevonden", market)
        return None

    gross_eur = available * current_price
    logger.info("[%s] LIVE SELL plaatsen — %.6f %s (~€%.2f) | reden: %s",
                market, available, symbol, gross_eur, reason)

    trade_id = save_live_trade(market, "SELL", None, current_price, available, gross_eur, "pending", reason)

    result = client.placeOrder(market, "sell", "market", {"amount": str(available)})

    if isinstance(result, dict) and "error" in result:
        update_live_trade(trade_id, current_price, available, gross_eur, "error")
        logger.error("[%s] LIVE SELL mislukt: %s", market, result["error"])
        return None

    order_id = result.get("orderId", "")
    filled = _poll_order(client, market, order_id)

    if filled and filled.get("status") == "filled":
        filled_price = float(filled.get("price") or current_price)
        filled_amount = float(filled.get("filledAmount", available))
        eur_total = float(filled.get("filledAmountQuote", gross_eur))

        buy_trades = [t for t in get_live_trades(market, limit=100)
                      if t["side"] == "BUY" and t["status"] == "filled"]
        avg_buy_price = (
            sum(t["price"] * t["amount"] for t in buy_trades) /
            sum(t["amount"] for t in buy_trades)
            if buy_trades else current_price
        )
        pnl = (filled_price - avg_buy_price) * filled_amount - (gross_eur * FEE_RATE)
        add_daily_pnl(market, pnl)

        update_live_trade(trade_id, filled_price, filled_amount, eur_total, "filled")
        logger.info(
            "[%s] LIVE SELL gevuld — prijs: €%.4f | bedrag: %.6f | PnL: €%.2f",
            market, filled_price, filled_amount, pnl,
        )
        return {"side": "SELL", "order_id": order_id, "price": filled_price,
                "amount": filled_amount, "eur": eur_total, "pnl": pnl}
    else:
        update_live_trade(trade_id, current_price, available, gross_eur, "timeout")
        logger.warning("[%s] LIVE SELL order niet gevuld binnen timeout", market)
        return None
