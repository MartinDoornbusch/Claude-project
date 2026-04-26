"""Flask web dashboard — portfolio, signalen en trades overzicht."""

from __future__ import annotations

import json
import os

from flask import Flask, render_template, jsonify, request, redirect, url_for

from src.database import (
    get_latest_signals, get_paper_trades, get_cash, get_position, get_ai_decisions,
    get_watchlist, get_enabled_markets, set_market_enabled, upsert_market_stats, save_market_advice,
    get_all_paper_trades_asc, get_daily_pnl_series, get_trading_paused, set_trading_paused,
)
from src.paper_trader import portfolio_value
from src.bitvavo_client import get_client
from src.portfolio import get_ticker_price
from src.ai_strategy import ai_enabled
from src.config_manager import read_config, write_config, config_from_form

app = Flask(__name__, template_folder="../templates")


@app.template_filter("fmt_price")
def fmt_price(value):
    """Adaptieve prijsopmaak: meer decimalen voor goedkope coins."""
    if value is None:
        return "—"
    v = float(value)
    if v == 0:
        return "€0"
    if v < 0.0001:
        return f"€{v:.8f}"
    if v < 0.01:
        return f"€{v:.6f}"
    if v < 1:
        return f"€{v:.4f}"
    if v < 10000:
        return f"€{v:,.2f}"
    return f"€{v:,.0f}"

# HA Add-on Ingress: zet SCRIPT_NAME op basis van de X-Ingress-Path header
# zodat url_for() correcte URLs genereert via de Ingress proxy.
class _IngressMiddleware:
    def __init__(self, wsgi_app):
        self._app = wsgi_app

    def __call__(self, environ, start_response):
        ingress_path = environ.get("HTTP_X_INGRESS_PATH", "").rstrip("/")
        if ingress_path:
            environ["SCRIPT_NAME"] = ingress_path
        return self._app(environ, start_response)

app.wsgi_app = _IngressMiddleware(app.wsgi_app)


@app.after_request
def allow_iframe(response):
    """Sta embedding in HA dashboard iframe toe."""
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    return response

def _dashboard_markets() -> list[str]:
    env_markets = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]
    try:
        m = get_enabled_markets()
        return m if m else env_markets
    except Exception:
        return env_markets


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
        ai_decisions = get_ai_decisions(limit=10) if ai_enabled() else []
    except Exception:
        ai_decisions = []

    from src.ai_provider import get_configured_providers
    active_providers = [p for p, _ in get_configured_providers()]

    return render_template(
        "index.html",
        portfolio=pf,
        market_data=market_data,
        trades=trades,
        markets=_dashboard_markets(),
        ai_enabled=ai_enabled(),
        ai_decisions=ai_decisions,
        active_providers=active_providers,
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
    # Herlaad .env direct in dit proces zodat API-aanroepen meteen de nieuwe waarden gebruiken
    from dotenv import load_dotenv
    from src.config_manager import ENV_PATH
    load_dotenv(dotenv_path=str(ENV_PATH), override=True)
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
    recs = [r for r in watchlist if r.get("ai_recommended")]
    if recs:
        ai_summary = f"{len(recs)} markten aanbevolen door AI."
    for row in watchlist:
        provider_votes: dict = {}
        reasoning = row.get("ai_reasoning") or ""
        if reasoning.startswith("{"):
            try:
                provider_votes = json.loads(reasoning)
            except Exception:
                pass
        row["provider_votes"] = provider_votes
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
        from src.ai_provider import get_configured_providers, get_active
        client = get_client()
        stats = get_market_stats(client)
        for m in stats:
            upsert_market_stats(m["market"], m["price"], m["change_24h"], m["volume_eur"])

        providers = get_configured_providers() or [get_active()]
        all_markets_set = {m["market"] for m in stats}

        vote_yes: dict[str, int] = {}
        vote_conf: dict[str, list] = {}
        per_prov_votes: dict[str, dict[str, dict]] = {}
        provider_results: dict = {}

        for prov, mdl in providers:
            try:
                advice = advise_markets(stats, provider=prov, model=mdl)
                rec_set = set(advice.get("recommended", []))
                provider_results[prov] = {
                    "summary": advice.get("summary", ""),
                    "recommended": list(rec_set),
                }
                for market in all_markets_set:
                    minfo = advice.get("markets", {}).get(market, {})
                    is_rec = market in rec_set
                    per_prov_votes.setdefault(market, {})[prov] = {
                        "yes": is_rec,
                        "reasoning": minfo.get("reasoning", "") if is_rec else "",
                    }
                    if is_rec:
                        vote_yes[market] = vote_yes.get(market, 0) + 1
                        vote_conf.setdefault(market, []).append(minfo.get("confidence") or 0.8)
            except Exception as exc:
                provider_results[prov] = {"error": str(exc)}

        n = len(providers)
        recommended: set[str] = set()
        for market in all_markets_set:
            yes = vote_yes.get(market, 0)
            prov_votes = per_prov_votes.get(market, {})
            reasoning_json = json.dumps(prov_votes) if prov_votes else ""
            if yes > n / 2:
                confs = vote_conf.get(market, [0.8])
                avg_conf = sum(confs) / len(confs)
                recommended.add(market)
                save_market_advice(market, True, avg_conf, reasoning_json)
            elif prov_votes:
                save_market_advice(market, False, None, reasoning_json)
            else:
                save_market_advice(market, False, None, "")

        summaries = [f"{p}: {d['summary']}" for p, d in provider_results.items() if "summary" in d]
        return jsonify({
            "ok": True,
            "recommended": list(recommended),
            "summary": "  |  ".join(summaries),
            "providers": provider_results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/real_portfolio")
def api_real_portfolio():
    try:
        from src.portfolio import get_portfolio_value_eur
        client = get_client()
        balances, total_eur = get_portfolio_value_eur(client)
        return jsonify({"balances": balances, "total_eur": round(total_eur, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trading/status")
def api_trading_status():
    return jsonify({"paused": get_trading_paused()})


@app.route("/api/trading/toggle", methods=["POST"])
def api_trading_toggle():
    paused = not get_trading_paused()
    set_trading_paused(paused)
    return jsonify({"paused": paused})


@app.route("/api/test_connection")
def api_test_connection():
    result: dict = {"bitvavo": False, "providers": {}, "errors": {}}

    try:
        client = get_client()
        t = client.time()
        if isinstance(t, dict) and "time" in t:
            result["bitvavo"] = True
        else:
            result["errors"]["bitvavo"] = str(t)
    except Exception as e:
        result["errors"]["bitvavo"] = str(e)

    try:
        from src.ai_provider import get_configured_providers, complete_for
        providers = get_configured_providers()
        if not providers:
            from src.ai_provider import get_active
            providers = [get_active()]
        for prov, mdl in providers:
            try:
                text = complete_for(prov, mdl, "Reply with the word OK and nothing else.", "ping", max_tokens=16)
                result["providers"][prov] = {"ok": bool(text.strip()), "model": mdl}
            except Exception as e:
                result["providers"][prov] = {"ok": False, "model": mdl, "error": str(e)}
    except Exception as e:
        result["errors"]["ai"] = str(e)

    return jsonify(result)


@app.route("/api/ai/google/models")
def api_google_models():
    try:
        from src.ai_provider import list_google_models
        return jsonify({"models": list_google_models()})
    except Exception as e:
        return jsonify({"error": str(e), "models": []}), 500


@app.route("/api/markets/available")
def api_markets_available():
    try:
        from src.market_scanner import get_all_eur_markets
        client = get_client()
        markets = get_all_eur_markets(client)
        return jsonify({"markets": markets})
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


@app.route("/optimize")
def optimize_page():
    markets = _dashboard_markets()
    return render_template("optimize.html", markets=markets)


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    try:
        from src.optimizer import run_optimization
        from src.candles import get_candles

        data     = request.get_json()
        market   = str(data.get("market", "BTC-EUR")).upper()
        interval = str(data.get("interval", "1h"))
        limit    = int(data.get("limit", 500))
        capital  = float(data.get("capital", 1000.0))

        client = get_client()
        df = get_candles(client, market, interval, limit=limit)
        if df is None or df.empty:
            return jsonify({"error": f"Geen candle-data voor {market}"}), 400

        results = run_optimization(df, market, interval, capital=capital)

        # Sla top-10 resultaten op in DB
        try:
            from src.database import save_backtest_run
            for r in results[:10]:
                save_backtest_run(
                    market=market, interval=interval,
                    sma_short=r["sma_short"], sma_long=r["sma_long"],
                    rsi_buy=r["rsi_buy"], rsi_sell=r["rsi_sell"],
                    capital=capital,
                    return_pct=r["return_pct"], sharpe=r["sharpe"],
                    max_dd=r["max_dd"], win_rate=r["win_rate"],
                    num_trades=r["num_trades"],
                )
        except Exception:
            pass

        return jsonify({"results": results, "total": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analytics")
def analytics_page():
    markets = _dashboard_markets()
    return render_template("analytics.html", markets=markets)


@app.route("/api/analytics")
def api_analytics():
    """Berekent PnL-pairs uit paper trades voor de analytics pagina."""
    try:
        from collections import defaultdict

        market = request.args.get("market")
        daily  = get_daily_pnl_series()

        def _build_pairs(trades: list) -> list:
            open_buys: dict[str, list] = {}
            result = []
            for t in trades:
                m = t["market"]
                if t["side"] == "BUY":
                    open_buys.setdefault(m, []).append(t)
                elif t["side"] == "SELL" and open_buys.get(m):
                    buy = open_buys[m].pop(0)
                    pnl_eur = t["eur_total"] - buy["eur_total"]
                    pnl_pct = (t["price"] - buy["price"]) / buy["price"] * 100
                    result.append({
                        "market":     m,
                        "buy_ts":     buy["ts"],
                        "sell_ts":    t["ts"],
                        "buy_price":  buy["price"],
                        "sell_price": t["price"],
                        "amount":     buy["amount"],
                        "pnl_eur":    round(pnl_eur, 4),
                        "pnl_pct":    round(pnl_pct, 3),
                    })
            return result

        # Alle pairs voor per-coin vergelijking (altijd ongefilterd)
        all_pairs = _build_pairs(get_all_paper_trades_asc(None))

        # Gefilterde pairs voor KPIs / grafiek
        pairs = _build_pairs(
            get_all_paper_trades_asc(market.upper() if market else None)
        ) if market else all_pairs

        # ── Per-coin breakdown ──────────────────────────────────────────────
        mkt_bucket: dict[str, list] = defaultdict(list)
        for p in all_pairs:
            mkt_bucket[p["market"]].append(p)

        per_market = []
        for mkt, mps in mkt_bucket.items():
            wins_m   = [p for p in mps if p["pnl_eur"] > 0]
            losses_m = [p for p in mps if p["pnl_eur"] <= 0]
            total    = sum(p["pnl_eur"] for p in mps)
            per_market.append({
                "market":      mkt,
                "num_trades":  len(mps),
                "total_pnl":   round(total, 2),
                "win_rate":    round(len(wins_m) / len(mps) * 100, 1) if mps else 0.0,
                "num_wins":    len(wins_m),
                "num_losses":  len(losses_m),
                "avg_pnl_eur": round(total / len(mps), 2) if mps else 0.0,
                "avg_pnl_pct": round(sum(p["pnl_pct"] for p in mps) / len(mps), 2) if mps else 0.0,
                "best_trade":  round(max((p["pnl_eur"] for p in mps), default=0), 2),
                "worst_trade": round(min((p["pnl_eur"] for p in mps), default=0), 2),
            })
        per_market.sort(key=lambda x: x["total_pnl"], reverse=True)

        # ── Cumulatieve equity curve ────────────────────────────────────────
        pairs_sorted = sorted(pairs, key=lambda x: x["sell_ts"])
        cum = 0.0
        equity = []
        for p in pairs_sorted:
            cum += p["pnl_eur"]
            equity.append({"ts": p["sell_ts"][:10], "cum_pnl": round(cum, 4)})

        # ── Totaal-samenvatting ─────────────────────────────────────────────
        wins   = [p for p in pairs if p["pnl_eur"] > 0]
        losses = [p for p in pairs if p["pnl_eur"] <= 0]

        return jsonify({
            "pairs":        pairs_sorted,
            "equity":       equity,
            "daily":        daily,
            "per_market":   per_market,
            "total_pnl":    round(sum(p["pnl_eur"] for p in pairs), 4),
            "num_trades":   len(pairs),
            "num_wins":     len(wins),
            "num_losses":   len(losses),
            "win_rate_pct": round(len(wins) / len(pairs) * 100, 1) if pairs else 0.0,
            "avg_win_eur":  round(sum(p["pnl_eur"] for p in wins)   / len(wins),   2) if wins   else 0.0,
            "avg_loss_eur": round(sum(p["pnl_eur"] for p in losses) / len(losses), 2) if losses else 0.0,
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
