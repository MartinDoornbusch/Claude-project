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
from src.database import init_db
from src.paper_trader import buy, sell, portfolio_value
from src.strategy import evaluate
from src.mqtt_publisher import publish_all

logger = logging.getLogger(__name__)

MARKETS = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]
INTERVAL = os.getenv("CANDLE_INTERVAL", "1h")
CHECK_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))


def run_cycle() -> None:
    """Één cyclus: haal data op, evalueer, handel op papier, publiceer naar MQTT."""
    logger.info("=== Cyclus gestart (%s) ===", ", ".join(MARKETS))
    client = get_client()
    market_signals: dict[str, dict] = {}
    market_prices: dict[str, float] = {}

    for market in MARKETS:
        try:
            df = get_candles(client, market, INTERVAL, limit=200)
            df = add_indicators(df)
            sig = latest_signals(df)
            signal = evaluate(market, INTERVAL, df)

            market_signals[market] = {**sig, "signal": signal}
            market_prices[market] = sig["close"]

            if signal == "BUY":
                buy(market, sig["close"], reason="Strategie: " + signal)
            elif signal == "SELL":
                sell(market, sig["close"], reason="Strategie: " + signal)

        except Exception as exc:
            logger.error("[%s] Fout tijdens cyclus: %s", market, exc, exc_info=True)

    pf = portfolio_value(market_prices)
    logger.info(
        "Portfolio: €%.2f cash + €%.2f posities = €%.2f totaal",
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

    logger.info("Bot gestart | markten: %s | interval: %s | check: elke %d min",
                ", ".join(MARKETS), INTERVAL, CHECK_MINUTES)

    init_db()

    # Draai direct één keer bij opstarten
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
