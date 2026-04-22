"""Flask web dashboard — portfolio, signalen en trades overzicht."""

from __future__ import annotations

import json
import os

from flask import Flask, render_template, jsonify, request

from src.database import get_latest_signals, get_paper_trades, get_cash, get_position, get_ai_decisions
from src.paper_trader import portfolio_value
from src.bitvavo_client import get_client
from src.portfolio import get_ticker_price
from src.ai_strategy import AI_ENABLED

app = Flask(__name__, template_folder="../templates")

MARKETS = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]


def _build_portfolio() -> dict:
    client = get_client()
    prices = {}
    for market in MARKETS:
        p = get_ticker_price(client, market)
        if p:
            prices[market] = p

    pf = portfolio_value(prices)
    return pf


def _build_market_data() -> list[dict]:
    rows = []
    for market in MARKETS:
        signals = get_latest_signals(market, limit=1)
        latest = signals[0] if signals else {}
        rows.append({
            "market": market,
            "price": latest.get("close"),
            "rsi": latest.get("rsi_14"),
            "sma_20": latest.get("sma_20"),
            "sma_50": latest.get("sma_50"),
            "signal": latest.get("signal", "—"),
            "ts": latest.get("ts", "—"),
        })
    return rows


@app.route("/")
def index():
    try:
        pf = _build_portfolio()
    except Exception:
        pf = {"cash_eur": 0, "total_eur": 0, "positions": {}}

    market_data = _build_market_data()
    trades = get_paper_trades(limit=20)

    ai_decisions = get_ai_decisions(limit=10) if AI_ENABLED else []

    return render_template(
        "index.html",
        portfolio=pf,
        market_data=market_data,
        trades=trades,
        markets=MARKETS,
        ai_enabled=AI_ENABLED,
        ai_decisions=ai_decisions,
    )


@app.route("/api/signals/<market>")
def api_signals(market: str):
    signals = get_latest_signals(market.upper(), limit=48)
    return jsonify(signals)


@app.route("/api/ai_decisions")
def api_ai_decisions():
    market = request.args.get("market")
    decisions = get_ai_decisions(market.upper() if market else None, limit=20)
    return jsonify(decisions)


@app.route("/api/portfolio")
def api_portfolio():
    try:
        pf = _build_portfolio()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(pf)


def start(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    app.run(host=host, port=port, debug=debug)
