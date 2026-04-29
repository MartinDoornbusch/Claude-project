"""Paper trading engine — virtueel kopen en verkopen zonder echt geld."""

from __future__ import annotations

import logging
import os
import random
import time

from src.env_utils import env_float
from src.database import (
    get_cash, set_cash,
    get_position, set_position,
    save_paper_trade,
    add_daily_pnl,
    get_all_positions,
)

logger = logging.getLogger(__name__)

# Simuleer 0.25% transactiekosten (zelfde als Bitvavo taker fee)
FEE_RATE = 0.0025


def _check_daily_loss(market: str) -> bool:
    """Returns True als dagelijkse verliesgrens bereikt is."""
    daily_loss_pct = env_float("DAILY_LOSS_LIMIT_PCT", 2.0)
    if daily_loss_pct <= 0:
        return False
    from src.database import get_total_daily_loss
    portfolio_basis  = env_float("PAPER_STARTING_CAPITAL", 1000)
    daily_loss_limit = portfolio_basis * daily_loss_pct / 100
    today_loss       = get_total_daily_loss()
    if today_loss < 0 and abs(today_loss) >= daily_loss_limit:
        logger.warning(
            "[%s] BUY geblokkeerd — dagelijkse verliesgrens %.1f%% (€%.2f) bereikt: €%.2f verlies vandaag",
            market, daily_loss_pct, daily_loss_limit, abs(today_loss),
        )
        return True
    return False


def _buy_iceberg(market: str, price: float, reason: str, total_spend: float) -> dict | None:
    """Smart iceberg koop — variabele chunks op basis van MIN_ICEBERG_CHUNK en ICEBERG_VARIANCE."""
    min_chunk = env_float("MIN_ICEBERG_CHUNK", 100.0)
    variance  = env_float("ICEBERG_VARIANCE", 0.15)
    interval  = env_float("ICEBERG_INTERVAL_SECONDS", 2)

    n          = max(2, int(total_spend // min_chunk))
    base_chunk = total_spend / n
    remaining  = total_spend

    total_amount = 0.0
    total_cost   = 0.0

    for i in range(n):
        is_last = (i == n - 1)

        if is_last:
            chunk_eur = remaining
        else:
            factor    = 1.0 + random.uniform(-variance, variance)
            chunk_eur = round(base_chunk * factor, 2)
            max_this  = remaining - (n - i - 1) * 5.0
            chunk_eur = max(5.0, min(chunk_eur, max_this))

        if chunk_eur < 5.0 or remaining < 5.0:
            break

        current_cash = get_cash()
        if current_cash < chunk_eur:
            logger.info("[%s] Iceberg chunk %d/%d gestopt — te weinig cash", market, i + 1, n)
            break

        fee    = chunk_eur * FEE_RATE
        amount = (chunk_eur - fee) / price

        set_cash(current_cash - chunk_eur)
        cur = get_position(market)
        if cur["amount"] > 0:
            new_total = cur["amount"] + amount
            new_avg   = (cur["amount"] * cur["avg_price"] + amount * price) / new_total
        else:
            new_total, new_avg = amount, price
        set_position(market, new_total, new_avg)

        total_amount += amount
        total_cost   += chunk_eur
        remaining    -= chunk_eur

        logger.info(
            "[%s] Iceberg chunk %d/%d — €%.2f | %.6f @ €%.4f",
            market, i + 1, n, chunk_eur, amount, price,
        )

        if not is_last and interval > 0:
            time.sleep(interval)

    if total_amount <= 0:
        return None

    save_paper_trade(market, "BUY", price, total_amount,
                     f"[Iceberg ×{n}] {reason}", planned_price=price)
    logger.info(
        "[%s] PAPER ICEBERG BUY — €%.2f | %d chunks | totaal: %.6f @ €%.4f",
        market, total_cost, n, total_amount, price,
    )
    return {"side": "BUY", "price": price, "amount": total_amount, "eur": total_cost}


def buy(market: str, price: float, reason: str = "", fraction: float | None = None) -> dict | None:
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

    trade_fraction = env_float("PAPER_TRADE_FRACTION", 0.15)
    used_fraction  = fraction if fraction is not None else trade_fraction
    spend_eur      = cash * used_fraction

    # Smart iceberg: alleen activeren boven de drempel
    iceberg_enabled   = os.getenv("ICEBERG_ENABLED", "false").lower() == "true"
    iceberg_threshold = env_float("ICEBERG_THRESHOLD", 500.0)
    if iceberg_enabled and spend_eur >= iceberg_threshold:
        return _buy_iceberg(market, price, reason, spend_eur)
    min_order = env_float("MIN_ORDER_EUR", 5.0)
    if spend_eur < min_order:
        if cash >= min_order:
            logger.info(
                "[%s] Ordergrootte verhoogd van €%.2f naar minimum €%.2f",
                market, spend_eur, min_order,
            )
            spend_eur = min_order
        else:
            logger.info(
                "[%s] BUY overgeslagen — cash €%.2f onder minimum order €%.2f",
                market, cash, min_order,
            )
            return None
    fee = spend_eur * FEE_RATE
    net_eur = spend_eur - fee
    amount = net_eur / price

    set_cash(cash - spend_eur)
    set_position(market, amount, price)
    save_paper_trade(market, "BUY", price, amount, reason, planned_price=price)

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
    save_paper_trade(market, "SELL", price, amount, reason, planned_price=price)

    logger.info(
        "[%s] PAPER SELL — prijs: €%.4f | bedrag: %.6f | opbrengst: €%.2f | PnL: €%.2f",
        market, price, amount, net_eur, pnl,
    )
    return {"side": "SELL", "price": price, "amount": amount, "eur": net_eur, "pnl": pnl}


def partial_sell(market: str, amount: float, price: float, reason: str = "") -> dict | None:
    """Verkoop een deel van de open positie (voor huisgeld-modus en portfolio-opschoning)."""
    position = get_position(market)
    if position["amount"] <= 0:
        return None

    amount    = min(amount, position["amount"])
    gross_eur = amount * price
    fee       = gross_eur * FEE_RATE
    net_eur   = gross_eur - fee
    remaining = position["amount"] - amount
    pnl       = net_eur - amount * position["avg_price"] / (1 - FEE_RATE)

    cash = get_cash()
    set_cash(cash + net_eur)

    if remaining > 1e-8:
        set_position(market, remaining, position["avg_price"])
    else:
        set_position(market, 0.0, 0.0)
        add_daily_pnl(market, pnl)

    save_paper_trade(market, "SELL", price, amount, reason, planned_price=price)
    logger.info(
        "[%s] PAPER PARTIAL SELL — prijs: €%.4f | bedrag: %.6f | opbrengst: €%.2f | resterend: %.6f",
        market, price, amount, net_eur, remaining,
    )
    return {"side": "SELL", "price": price, "amount": amount, "eur": net_eur,
            "partial": True, "remaining": remaining}


def portfolio_value(market_prices: dict[str, float]) -> dict:
    """
    Bereken de totale waarde van het paper portfolio.
    market_prices: {"BTC-EUR": 60000.0, ...}
    Posities in uitgeschakelde of overgeslagen markten worden altijd meegeteld
    (met inkoopprijs als fallback als er geen live prijs beschikbaar is).
    """
    cash = get_cash()
    positions = {}
    total = cash

    # Alle markten: actieve prijzen + markten met open posities (ook uitgeschakeld)
    all_markets = set(market_prices.keys())
    for row in get_all_positions():
        all_markets.add(row["market"])

    for market in all_markets:
        pos = get_position(market)
        if pos["amount"] > 0:
            price = market_prices.get(market, pos["avg_price"])
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
