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
    atr_sl_enabled    = os.getenv("ATR_SL_ENABLED", "false").lower() == "true"

    nothing_to_check = (
        stop_loss_pct is None and take_profit_pct is None
        and not trailing_enabled and breakeven_trigger is None
        and not atr_sl_enabled
    )
    if nothing_to_check:
        return False

    pos = get_position(market)
    if pos["amount"] <= 0 or pos["avg_price"] <= 0:
        return False

    avg_price = pos["avg_price"]
    chg_pct   = (current_price - avg_price) / avg_price * 100
    meta      = get_position_meta(market)

    # ── ATR-gebaseerde SL/TP: overschrijf vaste percentages ──────────────────
    if atr_sl_enabled:
        entry_atr = float(meta.get("entry_atr") or 0)
        if entry_atr > 0 and avg_price > 0:
            sl_mult = env_float("ATR_SL_MULTIPLIER", 1.5)
            tp_mult = env_float("ATR_TP_MULTIPLIER", 3.0)
            stop_loss_pct   = -(sl_mult * entry_atr / avg_price * 100)
            take_profit_pct =   tp_mult * entry_atr / avg_price * 100
            logger.debug(
                "[%s] ATR SL/TP: ATR=%.4f → SL=%.2f%% / TP=%.2f%%",
                market, entry_atr, stop_loss_pct, take_profit_pct,
            )

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
    fraction: float | None = None,
    entry_atr: float | None = None,
) -> dict | None:
    from src.notifier import notify_trade

    # Blacklist check
    blacklist = {m.strip().upper() for m in os.getenv("TRADING_BLACKLIST", "").split(",") if m.strip()}
    if market.upper() in blacklist:
        logger.info("[%s] BUY overgeslagen — markt staat op blacklist", market)
        return None

    # Maximaal aantal open posities
    max_pos = int(env_float("MAX_OPEN_POSITIONS", 0))
    if max_pos > 0:
        from src.database import get_all_positions
        open_count = len(get_all_positions())
        if open_count >= max_pos:
            logger.info(
                "[%s] BUY overgeslagen — max posities bereikt (%d/%d)",
                market, open_count, max_pos,
            )
            return None

    # Macro trend filter: blokkeer kopen als macro-markt in neerwaartse trend zit
    if os.getenv("MACRO_TREND_ENABLED", "false").lower() == "true":
        macro_market = os.getenv("MACRO_TREND_MARKET", "BTC-EUR").strip().upper()
        macro_sma_period = int(env_float("MACRO_TREND_SMA", 50))
        from src.database import get_latest_signals as _gls
        macro_sigs = _gls(macro_market, limit=1)
        if macro_sigs:
            ms = macro_sigs[0]
            macro_price = float(ms.get("close") or 0)
            macro_sma = float(ms.get("sma_50") or 0) if macro_sma_period >= 50 else float(ms.get("sma_20") or 0)
            if macro_price > 0 and macro_sma > 0 and macro_price < macro_sma:
                logger.info(
                    "[%s] BUY geblokkeerd — macro filter: %s €%.2f < SMA%d €%.2f (bearish)",
                    market, macro_market, macro_price, macro_sma_period, macro_sma,
                )
                return None

    # Winst-exclusiviteit: blokkeer nieuwe kopen als laatste trade verlies maakte
    if os.getenv("HOUSE_MONEY_ONLY_PROFIT", "false").lower() == "true":
        from src.env_utils import env_float as _ef
        live_mode = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
        if live_mode:
            from src.database import get_last_live_trade_pnl, get_last_live_sell_ts
            last_pnl = get_last_live_trade_pnl(market)
            last_ts  = get_last_live_sell_ts(market)
        else:
            from src.database import get_last_trade_pnl, get_last_sell_ts
            last_pnl = get_last_trade_pnl(market)
            last_ts  = get_last_sell_ts(market)
        if last_pnl is not None and last_pnl <= 0:
            cooldown_h = _ef("WIN_EXCL_COOLDOWN_HOURS", 6.0)
            blocked = True
            if cooldown_h > 0 and last_ts:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                _AMS = ZoneInfo("Europe/Amsterdam")
                sell_dt = datetime.fromisoformat(last_ts)
                if sell_dt.tzinfo is None:
                    sell_dt = sell_dt.replace(tzinfo=_AMS)
                elapsed_h = (datetime.now(_AMS) - sell_dt).total_seconds() / 3600
                if elapsed_h >= cooldown_h:
                    blocked = False
                    logger.info(
                        "[%s] Winst-exclusiviteit cooldown verstreken (%.1fh ≥ %.1fh) — BUY toegestaan",
                        market, elapsed_h, cooldown_h,
                    )
                else:
                    remaining = cooldown_h - elapsed_h
                    logger.info(
                        "[%s] BUY overgeslagen — winst-exclusiviteit: verlies €%.2f, nog %.1fh cooldown",
                        market, last_pnl, remaining,
                    )
            else:
                logger.info(
                    "[%s] BUY overgeslagen — winst-exclusiviteit: vorige trade verliesgevend (€%.2f)",
                    market, last_pnl,
                )
            if blocked:
                return None

    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        logger.info("[%s] Mode: LIVE — BUY uitvoeren", market)
        result = live.buy(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — BUY simuleren", market)
        result = paper.buy(market, price, reason, fraction=fraction)
    if result:
        notify_trade(market, "BUY", price, reason)
        from src.database import update_position_peak, cancel_all_oco_orders, set_entry_atr
        update_position_peak(market, price)
        cancel_all_oco_orders(market)
        if entry_atr and entry_atr > 0:
            set_entry_atr(market, entry_atr)
        # OCO: plaats TP + SL orders na een LIVE koop
        if (os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
                and os.getenv("OCO_ENABLED", "false").lower() == "true"):
            filled_amount = result.get("amount", 0)
            filled_price  = result.get("price", price)
            if filled_amount > 0:
                live.place_oco_orders(client, market, filled_amount, filled_price)
    return result


def check_dca(client: Bitvavo, market: str, current_price: float) -> bool:
    """
    Dollar Cost Averaging: koop bij als prijs DCA_THRESHOLD_PCT% onder gemiddelde inkoopprijs ligt
    en het maximale aantal DCA-lagen nog niet bereikt is.
    Geeft True terug als er bijgekocht is.
    """
    if os.getenv("DCA_ENABLED", "false").lower() != "true":
        return False

    pos = get_position(market)
    if pos["amount"] <= 0 or pos["avg_price"] <= 0:
        return False  # geen positie om bij te kopen

    avg = float(pos["avg_price"])
    drop_pct = (avg - current_price) / avg * 100
    threshold = env_float("DCA_THRESHOLD_PCT", 5.0)
    if drop_pct < threshold:
        return False

    # Tel hoeveel BUY-trades al open staan voor deze positie (DCA-lagen)
    from src.database import get_conn
    with get_conn() as conn:
        last_sell = conn.execute(
            "SELECT ts FROM paper_trades WHERE market=? AND side='SELL' ORDER BY ts DESC LIMIT 1",
            (market,)
        ).fetchone()
        since = last_sell["ts"] if last_sell else "1970-01-01"
        buy_count = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE market=? AND side='BUY' AND ts > ?",
            (market, since)
        ).fetchone()[0]

    max_layers = int(env_float("DCA_MAX_LAYERS", 2))
    if buy_count > max_layers:
        logger.info("[%s] DCA: max %d lagen bereikt (%d buys) — overgeslagen", market, max_layers, buy_count)
        return False

    logger.info(
        "[%s] DCA: prijs €%.4f is %.1f%% onder gemiddelde €%.4f — laag %d/%d bijkopen",
        market, current_price, drop_pct, avg, buy_count, max_layers,
    )
    result = execute_buy(client, market, current_price, reason=f"DCA laag {buy_count} (−{drop_pct:.1f}%)")
    return bool(result)


def _trigger_hodl_accumulation(client: Bitvavo, source_market: str, profit_eur: float) -> None:
    """
    Na een winstgevende verkoop: voeg winstdeel toe aan de buffer per HODL coin.
    Zodra de buffer ≥ MIN_ORDER_EUR is wordt de bijkoop uitgevoerd en de buffer gereset.
    """
    if profit_eur <= 0:
        return

    from src.database import get_all_hodl_configs, hodl_add_pending, hodl_reset_pending
    from src.portfolio import get_ticker_price
    from src.notifier import notify_trade

    min_order = env_float("MIN_ORDER_EUR", 5.0)

    for cfg in get_all_hodl_configs():
        if not cfg["enabled"]:
            continue
        market = cfg["market"]
        split_pct = float(cfg["accumulation_split_pct"])
        if split_pct <= 0:
            continue

        share_eur = profit_eur * split_pct / 100
        pending = hodl_add_pending(market, share_eur)

        if pending < min_order:
            logger.info(
                "[%s] HODL buffer +€%.2f → €%.2f (wacht op minimum €%.2f)",
                market, share_eur, pending, min_order,
            )
            continue

        price = get_ticker_price(client, market)
        if not price:
            logger.warning("[%s] HODL accum overgeslagen — prijs niet beschikbaar", market)
            continue

        reason = (
            f"HODL accum {split_pct:.0f}% ({source_market}) — buffer €{pending:.2f}"
        )
        if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
            result = live.accumulation_buy(client, market, price, reason, pending)
        else:
            result = paper.accumulation_buy(market, price, reason, pending)

        if result:
            hodl_reset_pending(market)
            from src.database import update_position_peak
            notify_trade(market, "BUY", price, reason)
            update_position_peak(market, price)
        else:
            logger.info("[%s] HODL bijkoop mislukt — buffer €%.2f bewaard", market, pending)


def execute_sell(client: Bitvavo, market: str, price: float, reason: str = "") -> dict | None:
    from src.notifier import notify_trade
    live_mode = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

    # HODL vloer: blokkeer of beperk verkoop om ingestelde minimumpositie te bewaren
    from src.database import get_hodl_config, get_position as _get_pos
    hodl_cfg = get_hodl_config(market)
    if hodl_cfg and hodl_cfg["enabled"] and float(hodl_cfg["floor_amount"]) > 0:
        floor = float(hodl_cfg["floor_amount"])

        if live_mode:
            symbol = market.split("-")[0]
            balances = client.balance({"symbol": symbol})
            current_amount = next(
                (float(b["available"]) for b in (balances if isinstance(balances, list) else [])
                 if b["symbol"] == symbol), 0.0
            )
            # Avg buy price voor PnL-berekening
            from src.database import get_live_trades as _glt
            buy_trades = [t for t in _glt(market, limit=100)
                          if t["side"] == "BUY" and t["status"] == "filled"]
            avg_buy_price = (
                sum(t["price"] * t["amount"] for t in buy_trades) /
                sum(t["amount"] for t in buy_trades)
                if buy_trades else price
            )
        else:
            pos = _get_pos(market)
            current_amount = pos["amount"]
            avg_buy_price = pos["avg_price"] if pos["avg_price"] > 0 else price

        sellable = current_amount - floor
        if sellable <= 1e-8:
            logger.info(
                "[%s] SELL geblokkeerd — HODL vloer %.8f beschermt volledige positie %.8f",
                market, floor, current_amount,
            )
            return None

        if sellable < current_amount - 1e-8:
            logger.info(
                "[%s] SELL beperkt door HODL vloer — verkoop %.8f (vloer: %.8f beschermd)",
                market, sellable, floor,
            )
            if live_mode:
                result = live.partial_sell(client, market, sellable, price, reason)
            else:
                result = paper.partial_sell(market, sellable, price, reason)
            if result:
                notify_trade(market, "SELL", price, reason)
                from src.database import cancel_all_oco_orders
                cancel_all_oco_orders(market)
                if live_mode:
                    live.cancel_exchange_oco_orders(client, market)
                fee_rate = live.FEE_RATE if live_mode else paper.FEE_RATE
                approx_pnl = sellable * (price - avg_buy_price) - sellable * price * fee_rate
                _trigger_hodl_accumulation(client, market, approx_pnl)
            return result

    if live_mode:
        logger.info("[%s] Mode: LIVE — SELL uitvoeren", market)
        result = live.sell(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — SELL simuleren", market)
        result = paper.sell(market, price, reason)
    if result:
        notify_trade(market, "SELL", price, reason)
        from src.database import cancel_all_oco_orders
        cancel_all_oco_orders(market)
        if live_mode:
            live.cancel_exchange_oco_orders(client, market)
        pnl = result.get("pnl") or 0.0
        _trigger_hodl_accumulation(client, market, pnl)
    return result
