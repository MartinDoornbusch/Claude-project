"""Scheduler — draait de strategie periodiek via APScheduler."""

from __future__ import annotations

import logging
import os
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.bitvavo_client import get_client
from src.candles import get_candles, latest_signals, add_indicators
from src.database import init_db, save_ai_decision, get_enabled_markets
from src.paper_trader import portfolio_value
from src.strategy import evaluate
from src.ai_strategy import AI_ENABLED, ai_evaluate
from src.mqtt_publisher import publish_all
from src.trade_manager import execute_buy, execute_sell, check_sl_tp, mode

logger = logging.getLogger(__name__)

INTERVAL = os.getenv("CANDLE_INTERVAL", "1h")
CHECK_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
_ENV_MARKETS = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]


def _active_markets() -> list[str]:
    """Geeft ingeschakelde markten uit de DB terug; valt terug op TRADING_MARKETS env-var."""
    try:
        markets = get_enabled_markets()
        return markets if markets else _ENV_MARKETS
    except Exception:
        return _ENV_MARKETS


def run_cycle() -> None:
    markets = _active_markets()
    logger.info("=== Cyclus gestart [%s] (%s) ===", mode(), ", ".join(markets))
    client = get_client()
    market_signals: dict[str, dict] = {}
    market_prices: dict[str, float] = {}

    for market in markets:
        try:
            df = get_candles(client, market, INTERVAL, limit=200)
            df = add_indicators(df)
            sig = latest_signals(df)
            current_price = sig["close"]

            # Stop-loss / take-profit check vóór strategie-evaluatie
            sl_tp_triggered = check_sl_tp(client, market, current_price)

            if AI_ENABLED:
                decision, confidence, reasoning = ai_evaluate(market, sig)
                executed = decision in ("BUY", "SELL")
                save_ai_decision(market, decision, confidence, reasoning, executed=executed)
                signal = decision
                reason = f"AI ({confidence:.0%}): {reasoning}"
            else:
                signal = evaluate(market, INTERVAL, df, client=client)
                reason = "MA crossover / RSI"

            market_signals[market] = {**sig, "signal": signal}
            market_prices[market] = current_price

            # Sla strategie-signal over als SL/TP al heeft verkocht
            if not sl_tp_triggered:
                if signal == "BUY":
                    execute_buy(client, market, current_price, reason=reason)
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
        "Bot gestart | modus: %s | markten: %s | interval: %s | check: elke %d min",
        mode(), ", ".join(_active_markets()), INTERVAL, CHECK_MINUTES,
    )

    if mode() == "LIVE":
        logger.warning("=" * 60)
        logger.warning("  LIVE TRADING ACTIEF — ECHTE ORDERS WORDEN GEPLAATST")
        logger.warning("  MAX_TRADE_EUR=%.2f  MAX_EXPOSURE_EUR=%.2f",
                       float(os.getenv("MAX_TRADE_EUR", "25")),
                       float(os.getenv("MAX_EXPOSURE_EUR", "100")))
        logger.warning("=" * 60)

    init_db()
    run_cycle()

    scheduler = BlockingScheduler(timezone="Europe/Amsterdam")
    scheduler.add_job(
        run_cycle,
        trigger=IntervalTrigger(minutes=CHECK_MINUTES),
        id="trading_cycle",
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(signum, frame):
        logger.info("Afsluiten...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    scheduler.start()
