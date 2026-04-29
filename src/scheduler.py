"""Scheduler — draait de strategie periodiek via APScheduler."""

from __future__ import annotations

import logging
import os
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.bitvavo_client import get_client
from src.candles import get_candles, latest_signals, add_indicators, get_atr_fraction, get_risk_fraction
from src.database import (
    init_db, save_ai_decision, save_signal, get_enabled_markets,
    save_portfolio_snapshot, get_trading_paused,
    get_latest_portfolio_total, get_cash,
)
from src.paper_trader import portfolio_value
from src.strategy import evaluate
from src.ai_strategy import ai_enabled, ai_evaluate
from src.mqtt_publisher import publish_all
from src.trade_manager import execute_buy, execute_sell, check_sl_tp, check_house_money, mode
from src.env_utils import env_float, env_int

logger = logging.getLogger(__name__)

_scheduler = None   # globale referentie voor herplanning


def _env_markets() -> list[str]:
    return [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]


def _active_markets() -> list[str]:
    """Geeft ingeschakelde markten terug, gefilterd op blacklist."""
    blacklist: set[str] = {m.strip().upper() for m in os.getenv("TRADING_BLACKLIST", "").split(",") if m.strip()}
    try:
        markets = get_enabled_markets()
        active  = markets if markets else _env_markets()
    except Exception:
        active = _env_markets()
    return [m for m in active if m.upper() not in blacklist]


def run_cycle() -> None:
    global _scheduler

    # Herlaad .env zodat live-wijzigingen via het dashboard meteen actief zijn
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

    # Lees alle config dynamisch
    interval        = os.getenv("CANDLE_INTERVAL", "1h")
    check_minutes   = env_int("CHECK_INTERVAL_MINUTES", 60)
    vol_sizing      = os.getenv("VOL_SIZING_ENABLED", "false").lower() == "true"
    corr_check      = os.getenv("CORR_CHECK_ENABLED", "false").lower() == "true"
    iceberg_enabled = os.getenv("ICEBERG_ENABLED", "false").lower() == "true"
    iceberg_chunks  = env_int("ICEBERG_CHUNKS", 5) if iceberg_enabled else 1

    # Auto-activeer risk-based sizing als STOP_LOSS_PCT + RISK_PER_TRADE_PCT beide zijn ingesteld
    from src.env_utils import env_float_opt
    _sl_set      = env_float_opt("STOP_LOSS_PCT") is not None
    _risk_set    = env_float("RISK_PER_TRADE_PCT", 0) > 0
    sizing_mode  = (
        "risk_pct" if (_sl_set and _risk_set)
        else os.getenv("POSITION_SIZING_MODE", "fraction")
    )

    # Portfolio totaal voor positiegroottes (gebruik laatste snapshot als startpunt)
    portfolio_total = get_latest_portfolio_total() or env_float("PAPER_STARTING_CAPITAL", 1000)

    # Herplan scheduler als interval gewijzigd is
    if _scheduler is not None:
        job = _scheduler.get_job("trading_cycle")
        if job:
            current_seconds = job.trigger.interval.total_seconds()
            if abs(current_seconds - check_minutes * 60) > 5:
                _scheduler.reschedule_job(
                    "trading_cycle",
                    trigger=IntervalTrigger(minutes=check_minutes),
                )
                logger.info("Scheduler herplanned: elke %d minuten", check_minutes)

    paused  = get_trading_paused()
    markets = _active_markets()
    logger.info(
        "=== Cyclus gestart [%s] (%s)%s ===",
        mode(), ", ".join(markets), " — TRADING GEPAUZEERD" if paused else "",
    )
    client = get_client()
    market_signals: dict[str, dict] = {}
    market_prices: dict[str, float] = {}

    for market in markets:
        try:
            df = get_candles(client, market, interval, limit=200)
            df = add_indicators(df)
            sig = latest_signals(df)
            current_price = sig["close"]

            # OCO leg-check (LIVE modus): annuleer tegengestelde leg als één gevuld is
            sl_tp_triggered = False
            if (not paused
                    and os.getenv("OCO_ENABLED", "false").lower() == "true"
                    and os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"):
                from src.live_trader import check_cancel_oco
                oco_result = check_cancel_oco(client, market)
                if oco_result:
                    sl_tp_triggered = True

            # Stop-loss / take-profit check — ook bij pauze (veiligheidsnet)
            if not sl_tp_triggered:
                sl_tp_triggered = check_sl_tp(client, market, current_price) if not paused else False

            # Huisgeld-check: verkoopt inleg terug als positie X% in de winst staat
            if not sl_tp_triggered and not paused:
                check_house_money(client, market, current_price)

            if ai_enabled():
                decision, confidence, reasoning = ai_evaluate(market, sig)
                executed = (not paused) and decision in ("BUY", "SELL")
                save_ai_decision(market, decision, confidence, reasoning, executed=executed)
                signal = decision
                reason = f"AI ({confidence:.0%}): {reasoning}"
            else:
                signal = evaluate(market, interval, df, client=client)
                reason = "MA crossover / RSI"

            # Sla indicatoren altijd op — ook bij AI-strategie (nodig voor grafieken)
            save_signal(market, interval, sig, signal)

            market_signals[market] = {**sig, "signal": signal}
            market_prices[market] = current_price

            if paused:
                logger.info("[%s] Signaal %s niet uitgevoerd — trading gepauzeerd", market, signal)
                continue

            # Sla strategie-signal over als SL/TP al heeft verkocht
            if not sl_tp_triggered:
                if signal == "BUY":
                    # Correlatie-check: voorkom dubbele blootstelling
                    if corr_check:
                        from src.correlation import has_correlated_position
                        blocked, corr_market = has_correlated_position(client, market, markets)
                        if blocked:
                            logger.info(
                                "[%s] BUY geblokkeerd: gecorreleerde positie open in %s",
                                market, corr_market,
                            )
                            signal = "HOLD"

                    if signal == "BUY":
                        # Positiegroottes op basis van gekozen methode
                        base_frac = env_float("PAPER_TRADE_FRACTION", 0.15)
                        if sizing_mode == "risk_pct":
                            fraction = get_risk_fraction(df, portfolio_total, get_cash(),
                                                         entry_price=current_price)
                        elif vol_sizing:
                            fraction = get_atr_fraction(df, base_frac)
                        else:
                            fraction = None
                        execute_buy(client, market, current_price, reason=reason,
                                    fraction=fraction, iceberg_chunks=iceberg_chunks)
                elif signal == "SELL":
                    execute_sell(client, market, current_price, reason=reason)

        except Exception as exc:
            logger.error("[%s] Fout tijdens cyclus: %s", market, exc, exc_info=True)

    pf = portfolio_value(market_prices)
    logger.info(
        "Paper portfolio: €%.2f cash + €%.2f posities = €%.2f totaal",
        pf["cash_eur"],
        pf["total_eur"] - pf["cash_eur"],
        pf["total_eur"],
    )

    try:
        save_portfolio_snapshot(
            cash_eur=pf["cash_eur"],
            pos_eur=pf["total_eur"] - pf["cash_eur"],
            total_eur=pf["total_eur"],
        )
    except Exception:
        pass

    try:
        publish_all(pf, market_signals)
    except Exception as exc:
        logger.warning("MQTT publish mislukt: %s", exc)


def start() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info(
        "Bot gestart | modus: %s | markten: %s | interval: %s | check: elke %s min",
        mode(), ", ".join(_active_markets()),
        os.getenv("CANDLE_INTERVAL", "1h"),
        os.getenv("CHECK_INTERVAL_MINUTES", "60"),
    )

    if mode() == "LIVE":
        logger.warning("=" * 60)
        logger.warning("  LIVE TRADING ACTIEF — ECHTE ORDERS WORDEN GEPLAATST")
        logger.warning("  MAX_TRADE_EUR=%.2f  MAX_EXPOSURE_EUR=%.2f",
                       env_float("MAX_TRADE_EUR", 25),
                       env_float("MAX_EXPOSURE_EUR", 100))
        logger.warning("=" * 60)

    init_db()
    run_cycle()

    global _scheduler
    check_minutes = env_int("CHECK_INTERVAL_MINUTES", 60)
    _scheduler = BlockingScheduler(timezone="Europe/Amsterdam")
    _scheduler.add_job(
        run_cycle,
        trigger=IntervalTrigger(minutes=check_minutes),
        id="trading_cycle",
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(signum, frame):
        logger.info("Afsluiten...")
        _scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _scheduler.start()
