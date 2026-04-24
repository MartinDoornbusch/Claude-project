"""Flask web dashboard — portfolio, signalen en trades overzicht."""

from __future__ import annotations

import json
import os

from flask import Flask, render_template, jsonify, request, redirect, url_for

from src.database import (
    get_latest_signals, get_paper_trades, get_cash, get_position, get_ai_decisions,
    get_watchlist, get_enabled_markets, set_market_enabled, upsert_market_stats, save_market_advice,
    get_all_paper_trades_asc, get_daily_pnl_series,
)
from src.paper_trader import portfolio_value
from src.bitvavo_client import get_client
from src.portfolio import get_ticker_price
from src.ai_strategy import AI_ENABLED
from src.config_manager import read_config, write_config, config_from_form

app = Flask(__name__, template_folder="../templates")

_ENV_MARKETS = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]


def _dashboard_markets() -> list[str]:
    try:
        m = get_enabled_markets()
        return m if m else _ENV_MARKETS
    except Exception:
        return _ENV_MARKETS


def _build_portfolio() -> dict:
    client = get_client()
    prices = {}
    for market in _dashboard_markets():
        p = get_ticker_price(client, market)
        if p:
            prices[market] = p

    pf = portfolio_value(prices)
    return pf


def _build_market_data() -> list[dict]:
    rows = []
    for market in _dashboard_markets():
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

    try:
        market_data = _build_market_data()
    except Exception:
        market_data = []

    try:
        trades = get_paper_trades(limit=20)
    except Exception:
        trades = []

    try:
        ai_decisions = get_ai_decisions(limit=10) if AI_ENABLED else []
    except Exception:
        ai_decisions = []

    return render_template(
        "index.html",
        portfolio=pf,
        market_data=market_data,
        trades=trades,
        markets=_dashboard_markets(),
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


@app.route("/settings", methods=["GET"])
def settings_page():
    config = read_config()
    saved = request.args.get("saved") == "1"
    return render_template("settings.html", config=config, saved=saved)


@app.route("/settings", methods=["POST"])
def settings_save():
    updates = config_from_form(request.form)
    write_config(updates)
    return redirect(url_for("settings_page", saved=1))


@app.route("/markets")
def markets_page():
    watchlist = get_watchlist()
    ai_summary = None
    ai_advised_at = None
    for row in watchlist:
        if row.get("last_advised"):
            ai_advised_at = row["last_advised"]
            break
    # Get last AI summary from most recent advised row
    advised = [r for r in watchlist if r.get("last_advised") and r.get("ai_reasoning")]
    # We store summary separately - use first recommended row's reasoning as proxy
    recs = [r for r in watchlist if r.get("ai_recommended") and r.get("ai_reasoning")]
    if recs:
        ai_summary = f"{len(recs)} markten aanbevolen door AI."
    return render_template(
        "markets.html",
        watchlist=watchlist,
        ai_summary=ai_summary,
        ai_advised_at=ai_advised_at,
    )


@app.route("/api/markets/scan")
def api_markets_scan():
    try:
        from src.market_scanner import get_market_stats
        client = get_client()
        stats = get_market_stats(client)
        for m in stats:
            upsert_market_stats(m["market"], m["price"], m["change_24h"], m["volume_eur"])
        return jsonify({"markets": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/markets/advise", methods=["POST"])
def api_markets_advise():
    try:
        from src.market_scanner import get_market_stats
        from src.ai_market_advisor import advise_markets
        client = get_client()
        stats = get_market_stats(client)
        for m in stats:
            upsert_market_stats(m["market"], m["price"], m["change_24h"], m["volume_eur"])

        advice = advise_markets(stats)
        recommended = set(advice.get("recommended", []))

        for market, info in advice.get("markets", {}).items():
            save_market_advice(
                market=market,
                recommended=info.get("include", False),
                confidence=info.get("confidence"),
                reasoning=info.get("reasoning", ""),
            )
        # Ensure all markets not in advice dict get ai_recommended=0
        all_markets = {m["market"] for m in stats}
        advised_markets = set(advice.get("markets", {}).keys())
        for market in all_markets - advised_markets:
            save_market_advice(market, False, None, "")

        return jsonify({"ok": True, "recommended": list(recommended), "summary": advice.get("summary", "")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/markets/toggle", methods=["POST"])
def api_markets_toggle():
    try:
        data = request.get_json()
        market = str(data["market"]).upper()
        enabled = bool(data["enabled"])
        set_market_enabled(market, enabled)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/analytics")
def analytics_page():
    markets = _dashboard_markets()
    return render_template("analytics.html", markets=markets)


@app.route("/api/analytics")
def api_analytics():
    """Berekent PnL-pairs uit paper trades voor de analytics pagina."""
    try:
        market = request.args.get("market")
        trades = get_all_paper_trades_asc(market.upper() if market else None)
        daily  = get_daily_pnl_series()

        # Match BUY→SELL pairs per market (FIFO)
        open_buys: dict[str, list] = {}
        pairs = []
        for t in trades:
            m = t["market"]
            if t["side"] == "BUY":
                open_buys.setdefault(m, []).append(t)
            elif t["side"] == "SELL" and open_buys.get(m):
                buy = open_buys[m].pop(0)
                pnl_eur = t["eur_total"] - buy["eur_total"]
                pnl_pct = (t["price"] - buy["price"]) / buy["price"] * 100
                pairs.append({
                    "market":      m,
                    "buy_ts":      buy["ts"],
                    "sell_ts":     t["ts"],
                    "buy_price":   buy["price"],
                    "sell_price":  t["price"],
                    "amount":      buy["amount"],
                    "pnl_eur":     round(pnl_eur, 4),
                    "pnl_pct":     round(pnl_pct, 3),
                })

        # Build cumulative equity from pairs (sorted by sell_ts)
        pairs_sorted = sorted(pairs, key=lambda x: x["sell_ts"])
        cum = 0.0
        equity = []
        for p in pairs_sorted:
            cum += p["pnl_eur"]
            equity.append({"ts": p["sell_ts"][:10], "cum_pnl": round(cum, 4)})

        # Summary stats
        total_pnl  = sum(p["pnl_eur"] for p in pairs)
        wins       = [p for p in pairs if p["pnl_eur"] > 0]
        losses     = [p for p in pairs if p["pnl_eur"] <= 0]
        win_rate   = round(len(wins) / len(pairs) * 100, 1) if pairs else 0.0
        avg_win    = round(sum(p["pnl_eur"] for p in wins) / len(wins), 2) if wins else 0.0
        avg_loss   = round(sum(p["pnl_eur"] for p in losses) / len(losses), 2) if losses else 0.0

        return jsonify({
            "pairs":        pairs_sorted,
            "equity":       equity,
            "daily":        daily,
            "total_pnl":    round(total_pnl, 4),
            "num_trades":   len(pairs),
            "num_wins":     len(wins),
            "num_losses":   len(losses),
            "win_rate_pct": win_rate,
            "avg_win_eur":  avg_win,
            "avg_loss_eur": avg_loss,
            "best_trade":   round(max((p["pnl_eur"] for p in pairs), default=0), 2),
            "worst_trade":  round(min((p["pnl_eur"] for p in pairs), default=0), 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/backtest")
def backtest_page():
    markets = _dashboard_markets()
    return render_template("backtest.html", markets=markets)


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    try:
        from src.backtester import run_backtest
        from src.candles import get_candles
        import dataclasses

        data = request.get_json()
        market   = str(data.get("market", "BTC-EUR")).upper()
        interval = str(data.get("interval", "1h"))
        limit    = int(data.get("limit", 500))
        capital  = float(data.get("capital", 1000.0))
        sl       = data.get("stop_loss_pct")
        tp       = data.get("take_profit_pct")

        client = get_client()
        df = get_candles(client, market, interval, limit=limit)
        if df is None or df.empty:
            return jsonify({"error": f"Geen candle-data beschikbaar voor {market}"}), 400

        result = run_backtest(
            df, market, interval,
            initial_capital=capital,
            stop_loss_pct=float(sl) if sl is not None else None,
            take_profit_pct=float(tp) if tp is not None else None,
        )

        # Serialize dataclass to dict
        d = dataclasses.asdict(result)
        # Trades: convert nested dataclasses already handled by asdict
        return jsonify(d)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def start(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    app.run(host=host, port=port, debug=debug)
