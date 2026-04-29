"""Live trader — echte orders plaatsen via Bitvavo met veiligheidslimieten."""

from __future__ import annotations

import logging
import os
import random
import time

from src.env_utils import env_float, env_float_opt

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

    max_trade_eur      = env_float("MAX_TRADE_EUR", 25)
    max_exposure_eur   = env_float("MAX_EXPOSURE_EUR", 100)
    daily_loss_pct     = env_float("DAILY_LOSS_LIMIT_PCT", 2.0)
    portfolio_basis    = env_float("PAPER_STARTING_CAPITAL", 1000)
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


def _buy_iceberg(client: Bitvavo, market: str, entry_price: float,
                 reason: str, total_spend: float) -> dict | None:
    """Smart LIVE iceberg — variabele chunks met slippage-guard en variance."""
    min_chunk  = env_float("MIN_ICEBERG_CHUNK", 100.0)
    variance   = env_float("ICEBERG_VARIANCE", 0.15)
    interval   = env_float("ICEBERG_INTERVAL_SECONDS", 2)
    slip_guard = env_float("ICEBERG_SLIPPAGE_GUARD_PCT", 0.5)

    n          = max(2, int(total_spend // min_chunk))
    base_chunk = total_spend / n
    remaining  = total_spend

    total_amount = 0.0
    total_cost   = 0.0

    for i in range(n):
        is_last = (i == n - 1)

        # Slippage guard: stop als prijs te ver is weggelopen van entry
        if slip_guard > 0 and i > 0:
            ticker = client.tickerPrice({"market": market})
            if isinstance(ticker, dict) and "price" in ticker:
                live_price = float(ticker["price"])
                drift_pct  = abs(live_price - entry_price) / entry_price * 100
                if drift_pct > slip_guard:
                    logger.warning(
                        "[%s] Iceberg gestopt chunk %d/%d — prijs drift %.2f%% > guard %.2f%%",
                        market, i + 1, n, drift_pct, slip_guard,
                    )
                    break

        # Variabele chunk-grootte
        if is_last:
            chunk_eur = remaining
        else:
            factor    = 1.0 + random.uniform(-variance, variance)
            chunk_eur = round(base_chunk * factor, 2)
            max_this  = remaining - (n - i - 1) * 5.0
            chunk_eur = max(5.0, min(chunk_eur, max_this))

        if chunk_eur < 5.0 or remaining < 5.0:
            break

        block = _guard_checks(client, market, chunk_eur)
        if block:
            logger.warning("[%s] Iceberg chunk %d/%d geblokkeerd: %s", market, i + 1, n, block)
            break

        logger.info("[%s] Iceberg chunk %d/%d — €%.2f", market, i + 1, n, chunk_eur)
        trade_id = save_live_trade(market, "BUY", None, entry_price, None, chunk_eur,
                                   "pending", f"[Iceberg {i+1}/{n}] {reason}")

        result = client.placeOrder(market, "buy", "market", {"amountQuote": str(chunk_eur)})

        if isinstance(result, dict) and "error" in result:
            update_live_trade(trade_id, entry_price, 0, chunk_eur, "error")
            logger.error("[%s] Iceberg chunk %d mislukt: %s", market, i + 1, result["error"])
            break

        order_id = result.get("orderId", "")
        filled   = _poll_order(client, market, order_id)

        if filled and filled.get("status") == "filled":
            f_price  = float(filled.get("price") or entry_price)
            f_amount = float(filled.get("filledAmount", 0))
            f_eur    = float(filled.get("filledAmountQuote", chunk_eur))
            update_live_trade(trade_id, f_price, f_amount, f_eur, "filled")
            total_amount += f_amount
            total_cost   += f_eur
            remaining    -= chunk_eur
            logger.info(
                "[%s] Iceberg chunk %d/%d gevuld — €%.2f | %.6f @ €%.4f",
                market, i + 1, n, f_eur, f_amount, f_price,
            )
        else:
            update_live_trade(trade_id, entry_price, 0, chunk_eur, "timeout")
            logger.warning("[%s] Iceberg chunk %d niet gevuld — stoppen", market, i + 1)
            break

        if not is_last and interval > 0:
            time.sleep(interval)

    if total_amount <= 0:
        return None

    avg_price = total_cost / total_amount
    logger.info(
        "[%s] LIVE ICEBERG BUY klaar — %d chunks | %.6f | gem. €%.4f | kosten: €%.2f",
        market, n, total_amount, avg_price, total_cost,
    )
    return {"side": "BUY", "price": avg_price, "amount": total_amount, "eur": total_cost}


def buy(client: Bitvavo, market: str, current_price: float, reason: str = "") -> dict | None:
    """
    Plaats een echte markt-koop order op Bitvavo.
    Gebruikt MAX_TRADE_EUR als orderbedrag; splitst automatisch als iceberg actief is.
    """
    spend_eur = env_float("MAX_TRADE_EUR", 25)
    min_order = env_float("MIN_ORDER_EUR", 5.0)
    if spend_eur < min_order:
        logger.warning(
            "[%s] LIVE BUY overgeslagen — MAX_TRADE_EUR €%.2f < MIN_ORDER_EUR €%.2f",
            market, spend_eur, min_order,
        )
        return None

    block = _guard_checks(client, market, spend_eur)
    if block:
        logger.warning("[%s] LIVE BUY geblokkeerd: %s", market, block)
        return None

    # Smart iceberg: alleen boven de drempel
    iceberg_enabled   = os.getenv("ICEBERG_ENABLED", "false").lower() == "true"
    iceberg_threshold = env_float("ICEBERG_THRESHOLD", 500.0)
    if iceberg_enabled and spend_eur >= iceberg_threshold:
        return _buy_iceberg(client, market, current_price, reason, spend_eur)

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


def partial_sell(client: Bitvavo, market: str, amount: float, current_price: float,
                 reason: str = "") -> dict | None:
    """Verkoop een specifieke hoeveelheid van de open positie (voor huisgeld-modus)."""
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true":
        logger.warning("[%s] LIVE PARTIAL SELL geblokkeerd: LIVE_TRADING_ENABLED niet true", market)
        return None

    gross_eur = amount * current_price
    logger.info("[%s] LIVE PARTIAL SELL — %.6f @ €%.4f | reden: %s", market, amount, current_price, reason)
    trade_id = save_live_trade(market, "SELL", None, current_price, amount, gross_eur, "pending", reason)

    result = client.placeOrder(market, "sell", "market", {"amount": str(amount)})
    if isinstance(result, dict) and "error" in result:
        update_live_trade(trade_id, current_price, amount, gross_eur, "error")
        logger.error("[%s] LIVE PARTIAL SELL mislukt: %s", market, result["error"])
        return None

    order_id = result.get("orderId", "")
    filled   = _poll_order(client, market, order_id)

    if filled and filled.get("status") == "filled":
        f_price  = float(filled.get("price") or current_price)
        f_amount = float(filled.get("filledAmount", amount))
        f_eur    = float(filled.get("filledAmountQuote", gross_eur))
        update_live_trade(trade_id, f_price, f_amount, f_eur, "filled")
        logger.info("[%s] LIVE PARTIAL SELL gevuld — %.6f @ €%.4f", market, f_amount, f_price)
        return {"side": "SELL", "order_id": order_id, "price": f_price,
                "amount": f_amount, "eur": f_eur, "partial": True}
    else:
        update_live_trade(trade_id, current_price, amount, gross_eur, "timeout")
        logger.warning("[%s] LIVE PARTIAL SELL niet gevuld binnen timeout", market)
        return None


def place_oco_orders(client, market: str, amount: float, entry_price: float) -> dict:
    """
    Na een BUY: plaatst takeProfit- en/of stopLoss-orders op Bitvavo.
    Slaat order-IDs op in oco_orders voor leg-annulering.
    """
    from src.database import save_oco_order

    tp_pct = env_float_opt("TAKE_PROFIT_PCT")
    sl_pct = env_float_opt("STOP_LOSS_PCT")

    tp_order_id: str | None = None
    sl_order_id: str | None = None
    tp_price: float | None = None
    sl_price: float | None = None

    if tp_pct and tp_pct > 0:
        tp_price = round(entry_price * (1 + tp_pct / 100), 8)
        r = client.placeOrder(market, "sell", "takeProfit", {
            "amount": str(amount), "triggerPrice": str(tp_price),
        })
        if isinstance(r, dict) and "orderId" in r:
            tp_order_id = r["orderId"]
            logger.info("[%s] OCO TP @ €%.4f — id: %s", market, tp_price, tp_order_id)
        else:
            logger.warning("[%s] OCO TP mislukt: %s", market, r)

    if sl_pct and sl_pct > 0:
        sl_price = round(entry_price * (1 - sl_pct / 100), 8)
        r = client.placeOrder(market, "sell", "stopLoss", {
            "amount": str(amount), "triggerPrice": str(sl_price),
        })
        if isinstance(r, dict) and "orderId" in r:
            sl_order_id = r["orderId"]
            logger.info("[%s] OCO SL @ €%.4f — id: %s", market, sl_price, sl_order_id)
        else:
            logger.warning("[%s] OCO SL mislukt: %s", market, r)

    if tp_order_id or sl_order_id:
        save_oco_order(market, amount, tp_order_id, sl_order_id, tp_price, sl_price)

    return {"tp_order_id": tp_order_id, "sl_order_id": sl_order_id,
            "tp_price": tp_price, "sl_price": sl_price}


def check_cancel_oco(client, market: str) -> str | None:
    """
    Controleert open OCO legs op Bitvavo. Als één leg gevuld is,
    annuleert de andere en update de DB. Retourneert 'TP', 'SL', of None.
    """
    from src.database import get_open_oco_orders, update_oco_status, clear_position_meta

    for oco in get_open_oco_orders(market):
        oco_id = oco["id"]
        tp_oid = oco.get("tp_order_id")
        sl_oid = oco.get("sl_order_id")
        tp_filled = sl_filled = False

        if tp_oid:
            try:
                o = client.getOrder(market, tp_oid)
                tp_filled = isinstance(o, dict) and o.get("status") == "filled"
            except Exception:
                pass

        if sl_oid:
            try:
                o = client.getOrder(market, sl_oid)
                sl_filled = isinstance(o, dict) and o.get("status") == "filled"
            except Exception:
                pass

        if tp_filled or sl_filled:
            leg     = "TP" if tp_filled else "SL"
            cancel  = sl_oid if tp_filled else tp_oid
            if cancel:
                try:
                    client.cancelOrder(market, cancel)
                except Exception:
                    pass
            update_oco_status(oco_id, f"filled_{leg.lower()}")
            clear_position_meta(market)
            logger.info("[%s] OCO %s gevuld — andere leg geannuleerd", market, leg)
            return leg

    return None
