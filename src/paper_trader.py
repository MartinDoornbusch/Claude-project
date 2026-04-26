"""Paper trading engine — virtueel kopen en verkopen zonder echt geld."""

from __future__ import annotations

import logging
import os

from src.database import (
    get_cash, set_cash,
    get_position, set_position,
    save_paper_trade,
    add_daily_pnl,
)

logger = logging.getLogger(__name__)

# Simuleer 0.25% transactiekosten (zelfde als Bitvavo taker fee)
FEE_RATE = 0.0025


def _check_daily_loss(market: str) -> bool:
    """Returns True als dagelijkse verliesgrens bereikt is."""
    daily_loss_pct = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "2.0"))
    if daily_loss_pct <= 0:
        return False
    from src.database import get_total_daily_loss
    portfolio_basis  = float(os.getenv("PAPER_STARTING_CAPITAL", "1000"))
    daily_loss_limit = portfolio_basis * daily_loss_pct / 100
    today_loss       = get_total_daily_loss()
    if today_loss < 0 and abs(today_loss) >= daily_loss_limit:
        logger.warning(
            "[%s] BUY geblokkeerd — dagelijkse verliesgrens %.1f%% (€%.2f) bereikt: €%.2f verlies vandaag",
            market, daily_loss_pct, daily_loss_limit, abs(today_loss),
        )
        return True
    return False


def _buy_iceberg(market: str, price: float, reason: str, used_fraction: float,
                 n_chunks: int, cash: float) -> dict | None:
    """Splits een koop op in n_chunks gelijke deelorders (iceberg)."""
    total_spend = cash * used_fraction
    chunk_eur   = total_spend / n_chunks

    total_amount = 0.0
    total_cost   = 0.0

    for i in range(n_chunks):
        fee     = chunk_eur * FEE_RATE
        net_eur = chunk_eur - fee
        amount  = net_eur / price

        current_cash = get_cash()
        if current_cash < chunk_eur:
            logger.info("[%s] Iceberg chunk %d/%d overgeslagen — te weinig cash", market, i + 1, n_chunks)
            break

        set_cash(current_cash - chunk_eur)

        total_amount += amount
        total_cost   += chunk_eur

        current_pos = get_position(market)
        if current_pos["amount"] > 0:
            new_total  = current_pos["amount"] + amount
            new_avg    = (current_pos["amount"] * current_pos["avg_price"] + amount * price) / new_total
        else:
            new_total  = amount
            new_avg    = price
        set_position(market, new_total, new_avg)

        logger.info(
            "[%s] Iceberg chunk %d/%d — €%.2f | %.6f @ €%.4f",
            market, i + 1, n_chunks, chunk_eur, amount, price,
        )

    if total_amount <= 0:
        return None

    iceberg_reason = f"[Iceberg {n_chunks}×] {reason}"
    save_paper_trade(market, "BUY", price, total_amount, iceberg_reason)
    logger.info(
        "[%s] PAPER ICEBERG BUY — prijs: €%.4f | totaal: %.6f | kosten: €%.2f | %d chunks",
        market, price, total_amount, total_cost, n_chunks,
    )
    return {"side": "BUY", "price": price, "amount": total_amount, "eur": total_cost}


def buy(market: str, price: float, reason: str = "", fraction: float | None = None,
        iceberg_chunks: int = 1) -> dict | None:
    """
    Simuleer een marktorder koop.
    Gebruikt fraction (of PAPER_TRADE_FRACTION als None) van beschikbaar cash.
    Retourneert trade-info of None als er niets te besteden is.
    """
    cash = get_cash()
    if cash < 1.0:
        logger.info("[%s] BUY overgeslagen — te weinig cash (€%.2f)", market, cash)
        return None

    if _check_daily_loss(market):
        return None

    position = get_position(market)
    if position["amount"] > 0:
        logger.info("[%s] BUY overgeslagen — positie al open (%.6f)", market, position["amount"])
        return None

    trade_fraction = float(os.getenv("PAPER_TRADE_FRACTION", "0.15"))
    used_fraction  = fraction if fraction is not None else trade_fraction

    if iceberg_chunks > 1:
        return _buy_iceberg(market, price, reason, used_fraction, iceberg_chunks, cash)

    spend_eur = cash * used_fraction
    fee = spend_eur * FEE_RATE
    net_eur = spend_eur - fee
    amount = net_eur / price

    set_cash(cash - spend_eur)
    set_position(market, amount, price)
    save_paper_trade(market, "BUY", price, amount, reason)

    logger.info(
        "[%s] PAPER BUY  — prijs: €%.4f | bedrag: %.6f | kosten: €%.2f | fee: €%.4f | fractie: %.0f%%",
        market, price, amount, spend_eur, fee, used_fraction * 100,
    )
    return {"side": "BUY", "price": price, "amount": amount, "eur": spend_eur}


def sell(market: str, price: float, reason: str = "") -> dict | None:
    """
    Simuleer een marktorder verkoop van de volledige positie.
    Retourneert trade-info of None als er geen positie is.
    """
    position = get_position(market)
    if position["amount"] <= 0:
        logger.info("[%s] SELL overgeslagen — geen open positie", market)
        return None

    amount = position["amount"]
    gross_eur = amount * price
    fee = gross_eur * FEE_RATE
    net_eur = gross_eur - fee

    avg_price = position["avg_price"]
    cost_basis = amount * avg_price / (1 - FEE_RATE)
    pnl = net_eur - cost_basis

    cash = get_cash()
    set_cash(cash + net_eur)
    set_position(market, 0.0, 0.0)
    add_daily_pnl(market, pnl)
    save_paper_trade(market, "SELL", price, amount, reason)

    logger.info(
        "[%s] PAPER SELL — prijs: €%.4f | bedrag: %.6f | opbrengst: €%.2f | PnL: €%.2f",
        market, price, amount, net_eur, pnl,
    )
    return {"side": "SELL", "price": price, "amount": amount, "eur": net_eur, "pnl": pnl}


def portfolio_value(market_prices: dict[str, float]) -> dict:
    """
    Bereken de totale waarde van het paper portfolio.
    market_prices: {"BTC-EUR": 60000.0, ...}
    """
    cash = get_cash()
    positions = {}
    total = cash

    for market, price in market_prices.items():
        pos = get_position(market)
        if pos["amount"] > 0:
            eur_value = pos["amount"] * price
            total += eur_value
            positions[market] = {
                "amount": pos["amount"],
                "avg_price": pos["avg_price"],
                "current_price": price,
                "eur_value": eur_value,
                "pnl": eur_value - (pos["amount"] * pos["avg_price"]),
            }

    return {"cash_eur": cash, "positions": positions, "total_eur": total}
