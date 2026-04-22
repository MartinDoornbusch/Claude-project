"""Trade manager — schakelt transparant tussen paper en live trading."""

from __future__ import annotations

import logging
import os

from python_bitvavo_api.bitvavo import Bitvavo

import src.paper_trader as paper
import src.live_trader as live

logger = logging.getLogger(__name__)

LIVE_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"


def mode() -> str:
    return "LIVE" if LIVE_ENABLED else "PAPER"


def execute_buy(client: Bitvavo, market: str, price: float, reason: str = "") -> dict | None:
    if LIVE_ENABLED:
        logger.info("[%s] Mode: LIVE — BUY uitvoeren", market)
        return live.buy(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — BUY simuleren", market)
        return paper.buy(market, price, reason)


def execute_sell(client: Bitvavo, market: str, price: float, reason: str = "") -> dict | None:
    if LIVE_ENABLED:
        logger.info("[%s] Mode: LIVE — SELL uitvoeren", market)
        return live.sell(client, market, price, reason)
    else:
        logger.info("[%s] Mode: PAPER — SELL simuleren", market)
        return paper.sell(market, price, reason)
