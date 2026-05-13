"""Flask web dashboard — portfolio, signalen en trades overzicht."""

from __future__ import annotations

import json
import os

from flask import Flask, render_template, jsonify, request, redirect, url_for, Response

from src.database import (
    get_latest_signals, get_paper_trades, get_cash, get_position, get_ai_decisions,
    get_watchlist, get_enabled_markets, set_market_enabled, upsert_market_stats, save_market_advice,
    get_all_paper_trades_asc, get_daily_pnl_series, get_trading_paused, set_trading_paused,
    get_live_trades, reset_paper_trading, get_portfolio_snapshots,
    get_all_positions, get_total_daily_loss, get_latest_portfolio_total, get_position_meta,
    get_last_buy_ts, get_total_fees_paid,
)
from src.paper_trader import portfolio_value
from src.bitvavo_client import get_client
from src.portfolio import get_ticker_price
from src.ai_strategy import ai_enabled, classify_market
from src.config_manager import read_config, write_config, config_from_form
from src.env_utils import env_float

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
    """Sta embedding in HA dashboard iframe toe + voorkom proxy-caching van API-responses."""
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
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


def _parse_ai_reasoning(reasoning: str) -> dict[str, str]:
    """Parse '[provider] text | [provider] text' format into a per-provider dict."""
    import re
    result: dict[str, str] = {}
    for part in re.split(r"\s*\|\s*", reasoning or ""):
        m = re.match(r"\[(\w+)\]\s*(.*)", part.strip())
        if m:
            result[m.group(1).lower()] = m.group(2).strip()
    return result


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
            "market_class": classify_market(market),
        })
    return rows


@app.route("/")
def index():
    live_mode = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

    try:
        pf = _build_portfolio()
    except Exception:
        pf = {"cash_eur": 0, "total_eur": 0, "positions": {}}

    try:
        market_data = _build_market_data()
    except Exception:
        market_data = []

    try:
        trades = get_live_trades(limit=20) if live_mode else get_paper_trades(limit=20)
    except Exception:
        trades = []

    try:
        ai_decisions = get_ai_decisions(limit=10) if ai_enabled() else []
    except Exception:
        ai_decisions = []

    from src.ai_provider import get_configured_providers
    active_providers = [p for p, _ in get_configured_providers()]

    # ── Daily Loss Risk Meter ──────────────────────────────────────────────────
    daily_loss_data: dict = {"loss_eur": 0.0, "limit_eur": 0.0, "limit_pct": 2.0, "pct_used": 0.0}
    try:
        loss_eur  = get_total_daily_loss()  # negative = loss
        limit_pct = env_float("DAILY_LOSS_LIMIT_PCT", 2.0)
        ptotal    = get_latest_portfolio_total() or env_float("PAPER_STARTING_CAPITAL", 1000)
        limit_eur = ptotal * limit_pct / 100
        pct_used  = min(100.0, abs(loss_eur) / limit_eur * 100) if limit_eur > 0 and loss_eur < 0 else 0.0
        daily_loss_data = {
            "loss_eur":  round(loss_eur, 2),
            "limit_eur": round(limit_eur, 2),
            "limit_pct": limit_pct,
            "pct_used":  round(pct_used, 1),
        }
    except Exception:
        pass

    # ── AI Votes per market ────────────────────────────────────────────────────
    ai_votes_by_market: dict = {}
    if ai_enabled():
        try:
            for market in _dashboard_markets():
                decisions = get_ai_decisions(market, limit=1)
                if decisions:
                    d = decisions[0]
                    ai_votes_by_market[market] = {
                        "decision":   d["decision"],
                        "confidence": d["confidence"],
                        "ts":         d["ts"],
                        "providers":  _parse_ai_reasoning(d.get("reasoning", "")),
                    }
        except Exception:
            pass

    # ── Open Positions SL/TP per market ───────────────────────────────────────
    positions_data: dict = {}
    try:
        sl_pct           = env_float("STOP_LOSS_PCT", 0.0)
        tp_pct           = env_float("TAKE_PROFIT_PCT", 0.0)
        trailing_enabled = os.getenv("TRAILING_STOP_ENABLED", "false").lower() == "true"
        trailing_pct     = env_float("TRAILING_STOP_PCT", 2.0)
        for market in _dashboard_markets():
            pos = get_position(market)
            if pos["amount"] > 0 and pos["avg_price"] > 0:
                avg      = pos["avg_price"]
                sl_price = avg * (1 - sl_pct / 100) if sl_pct > 0 else None
                tp_price = avg * (1 + tp_pct / 100) if tp_pct > 0 else None
                meta     = get_position_meta(market)
                peak     = meta.get("peak_price") or 0.0
                t_sl     = peak * (1 - trailing_pct / 100) if trailing_enabled and peak > 0 else None
                candidates   = [x for x in [sl_price, t_sl] if x is not None]
                effective_sl = max(candidates) if candidates else None
                try:
                    buy_ts = get_last_buy_ts(market)
                except Exception:
                    buy_ts = None
                positions_data[market] = {
                    "amount":          pos["amount"],
                    "avg_price":       avg,
                    "sl_price":        effective_sl,
                    "tp_price":        tp_price,
                    "trailing_active": trailing_enabled and peak > 0,
                    "house_money":     bool(meta.get("house_money_activated")),
                    "buy_ts":          buy_ts,
                }
    except Exception:
        pass

    # ── Groq Token Gauge ──────────────────────────────────────────────────────
    groq_tokens_data: dict = {"used": 0, "limit": 500_000, "pct_used": 0.0}
    if "groq" in active_providers:
        try:
            from src.database import get_groq_daily_tokens
            used      = get_groq_daily_tokens()
            limit     = 500_000
            pct_used  = min(100.0, used / limit * 100) if limit > 0 else 0.0
            groq_tokens_data = {"used": used, "limit": limit, "pct_used": round(pct_used, 1)}
        except Exception:
            pass

    # ── Google Request Gauge ──────────────────────────────────────────────────
    google_requests_data: dict = {"used": 0, "limit": 1_500, "pct_used": 0.0}
    if "google" in active_providers:
        try:
            from src.database import get_google_daily_requests
            limit    = int(os.getenv("GOOGLE_DAILY_LIMIT", "1500"))
            used     = get_google_daily_requests()
            pct_used = min(100.0, used / limit * 100) if limit > 0 else 0.0
            google_requests_data = {"used": used, "limit": limit, "pct_used": round(pct_used, 1)}
        except Exception:
            pass

    # ── Mistral Token Gauge ───────────────────────────────────────────────────
    mistral_tokens_data: dict = {"used": 0, "limit": 500_000, "pct_used": 0.0}
    if "mistral" in active_providers:
        try:
            from src.database import get_mistral_daily_tokens
            used     = get_mistral_daily_tokens()
            limit    = int(os.getenv("MISTRAL_DAILY_LIMIT", "500000"))
            pct_used = min(100.0, used / limit * 100) if limit > 0 else 0.0
            mistral_tokens_data = {"used": used, "limit": limit, "pct_used": round(pct_used, 1)}
        except Exception:
            pass

    # ── Cerebras Token Gauge ──────────────────────────────────────────────────
    cerebras_tokens_data: dict = {"used": 0, "limit": 1_000_000, "pct_used": 0.0}
    if "cerebras" in active_providers:
        try:
            from src.database import get_cerebras_daily_tokens
            used     = get_cerebras_daily_tokens()
            limit    = int(os.getenv("CEREBRAS_DAILY_LIMIT", "1000000"))
            pct_used = min(100.0, used / limit * 100) if limit > 0 else 0.0
            cerebras_tokens_data = {"used": used, "limit": limit, "pct_used": round(pct_used, 1)}
        except Exception:
            pass

    try:
        total_fees = get_total_fees_paid()
    except Exception:
        total_fees = 0.0

    starting_capital = env_float("PAPER_STARTING_CAPITAL", 1000.0)

    return render_template(
        "index.html",
        live_mode=live_mode,
        portfolio=pf,
        market_data=market_data,
        trades=trades,
        markets=_dashboard_markets(),
        ai_enabled=ai_enabled(),
        ai_decisions=ai_decisions,
        active_providers=active_providers,
        daily_loss=daily_loss_data,
        ai_votes_by_market=ai_votes_by_market,
        positions_data=positions_data,
        groq_tokens=groq_tokens_data,
        google_requests=google_requests_data,
        mistral_tokens=mistral_tokens_data,
        cerebras_tokens=cerebras_tokens_data,
        total_fees=total_fees,
        starting_capital=starting_capital,
    )


@app.route("/api/signals/<market>")
def api_signals(market: str):
    market = market.upper()
    signals = get_latest_signals(market, limit=48)
    if not signals:
        # Geen opgeslagen data — haal candles on-demand op voor directe weergave
        try:
            from src.candles import get_candles, add_indicators, latest_signals as _latest
            from src.database import save_signal
            client = get_client()
            interval = os.getenv("CANDLE_INTERVAL", "1h")
            df  = get_candles(client, market, interval, limit=48)
            df  = add_indicators(df)
            for i in range(max(1, len(df) - 47), len(df) + 1):
                sig = _latest(df.iloc[:i].copy())
                save_signal(market, interval, sig, None)
            signals = get_latest_signals(market, limit=48)
        except Exception:
            pass
    return jsonify(signals)


@app.route("/api/ai_decisions")
def api_ai_decisions():
    market = request.args.get("market")
    decisions = get_ai_decisions(market.upper() if market else None, limit=20)
    return jsonify(decisions)


@app.route("/api/ai/accuracy")
def api_ai_accuracy():
    from src.database import get_ai_accuracy_stats
    return jsonify(get_ai_accuracy_stats())


@app.route("/api/portfolio")
def api_portfolio():
    try:
        pf = _build_portfolio()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(pf)


@app.route("/api/portfolio/history")
def api_portfolio_history():
    """Portfolio waarde over tijd — voor de groeigrafiek op het dashboard."""
    try:
        limit  = int(request.args.get("limit", 500))
        snaps  = get_portfolio_snapshots(limit=limit)
        start  = float(os.getenv("PAPER_STARTING_CAPITAL", "1000"))
        result = []
        for s in snaps:
            total = s["total_eur"]
            result.append({
                "ts":       s["ts"][:16],
                "total":    round(total, 2),
                "cash":     round(s["cash_eur"], 2),
                "pos":      round(s["pos_eur"], 2),
                "growth_pct": round((total - start) / start * 100, 2) if start > 0 else 0,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/heartbeat")
def api_heartbeat():
    """Laatste bot-cyclus tijdstempel — voor de heartbeat-indicator op het dashboard."""
    try:
        import datetime
        snaps = get_portfolio_snapshots(limit=1)
        if not snaps:
            return jsonify({"status": "unknown", "minutes_ago": None, "last_ts": None})
        last_ts_str = snaps[0]["ts"]
        last_ts = datetime.datetime.fromisoformat(last_ts_str)
        now = datetime.datetime.utcnow()
        minutes_ago = round((now - last_ts).total_seconds() / 60, 1)
        check_minutes = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
        if minutes_ago <= check_minutes * 1.5:
            status = "ok"
        elif minutes_ago <= check_minutes * 3:
            status = "warning"
        else:
            status = "stale"
        return jsonify({
            "status": status,
            "minutes_ago": minutes_ago,
            "last_ts": last_ts_str[:16],
            "check_interval": check_minutes,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio/cleanup", methods=["POST"])
def api_portfolio_cleanup():
    try:
        data           = request.get_json(force=True, silent=True) or {}
        cleanup_pct    = float(data.get("pct", os.getenv("CLEANUP_PCT", "50"))) / 100
        blacklist      = {m.strip().upper() for m in os.getenv("TRADING_BLACKLIST", "").split(",") if m.strip()}
        active_markets = {m.strip().upper() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",") if m.strip()}
        client         = get_client()
        results        = []

        if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
            balances = client.balance({})
            if not isinstance(balances, list):
                return jsonify({"error": "Kon balances niet ophalen"}), 500
            for b in balances:
                symbol = b.get("symbol", "")
                if not symbol or symbol == "EUR":
                    continue
                market      = f"{symbol}-EUR"
                if market in active_markets or market in blacklist:
                    continue
                available = float(b.get("available", 0))
                if available < 1e-8:
                    continue
                sell_amount = available * cleanup_pct
                price_data  = client.tickerPrice({"market": market})
                price       = float(price_data.get("price", 0)) if isinstance(price_data, dict) else 0
                r = client.placeOrder(market, "sell", "market", {"amount": str(sell_amount)})
                results.append({"market": market, "amount": round(sell_amount, 6), "price": price,
                                 "ok": isinstance(r, dict) and "error" not in r})
        else:
            from src.paper_trader import partial_sell
            for pos in get_all_positions():
                market = pos["market"]
                if market in active_markets or market in blacklist:
                    continue
                price = get_ticker_price(client, market)
                if not price:
                    continue
                sell_amount = pos["amount"] * cleanup_pct
                r = partial_sell(market, sell_amount, price, f"Portfolio opschoning {int(cleanup_pct*100)}%")
                results.append({"market": market, "amount": round(sell_amount, 6), "price": price, "ok": bool(r)})

        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio/manual_sell", methods=["POST"])
def api_portfolio_manual_sell():
    try:
        data   = request.get_json(force=True, silent=True) or {}
        market = (data.get("market") or "").strip().upper()
        if not market:
            return jsonify({"error": "market verplicht"}), 400

        if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
            live_client = get_client()
            price_data = live_client.tickerPrice({"market": market})
            price = float(price_data.get("price", 0)) if isinstance(price_data, dict) else 0
            if not price:
                return jsonify({"error": "Kon prijs niet ophalen"}), 500
            pos = get_position(market)
            if pos["amount"] <= 0:
                return jsonify({"error": "Geen open positie"}), 400
            r = live_client.placeOrder(market, "sell", "market", {"amount": str(pos["amount"])})
            if not isinstance(r, dict) or "error" in r:
                return jsonify({"error": str(r)}), 500
            return jsonify({"ok": True, "market": market, "eur": pos["amount"] * price, "pnl": 0})
        else:
            from src.paper_trader import sell as paper_sell
            price = get_ticker_price(get_client(), market)
            if not price:
                return jsonify({"error": "Kon prijs niet ophalen"}), 500
            result = paper_sell(market, price, "Handmatige verkoop")
            if not result:
                return jsonify({"error": "Geen open positie voor " + market}), 400
            return jsonify({"ok": True, "market": market, "eur": result["eur"], "pnl": result["pnl"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    capital = env_float("PAPER_STARTING_CAPITAL", 1000)
    reset_paper_trading(capital)
    return jsonify({"ok": True, "capital": capital})


@app.route("/api/orderbook/<market>")
def api_orderbook(market: str):
    try:
        client = get_client()
        book = client.book(market.upper(), {"depth": 25})
        if isinstance(book, dict) and "error" in book:
            return jsonify({"error": book["error"]}), 500
        bids = [[float(p), float(q)] for p, q in (book.get("bids") or [])[:15]]
        asks = [[float(p), float(q)] for p, q in (book.get("asks") or [])[:15]]
        return jsonify({"bids": bids, "asks": asks, "market": market.upper()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/settings", methods=["GET"])
def settings_page():
    config = read_config()
    saved = request.args.get("saved") == "1"
    from src.ai_provider import get_configured_providers
    active_providers = [p for p, _ in get_configured_providers()]
    return render_template("settings.html", config=config, saved=saved, active_providers=active_providers)


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
        row["market_class"] = classify_market(row["market"])
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

        # Google/Gemini heeft een strikt dagquotum — niet gebruiken voor bulk marktadvies
        providers = [(p, m) for p, m in (get_configured_providers() or [get_active()])
                     if p != "google"]
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


@app.route("/api/ai/groq/models")
def api_groq_models():
    try:
        from src.ai_provider import list_groq_models
        return jsonify({"models": list_groq_models()})
    except Exception as e:
        return jsonify({"error": str(e), "models": []}), 500


@app.route("/api/ai/mistral/models")
def api_mistral_models():
    try:
        from src.ai_provider import list_mistral_models
        return jsonify({"models": list_mistral_models()})
    except Exception as e:
        return jsonify({"error": str(e), "models": []}), 500


@app.route("/api/ai/cerebras/models")
def api_cerebras_models():
    try:
        from src.ai_provider import list_cerebras_models
        return jsonify({"models": list_cerebras_models()})
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
                    planned = buy.get("planned_price")
                    if planned and planned > 0 and buy["price"] > 0:
                        slippage_pct = round((planned - buy["price"]) / planned * 100, 3)
                    else:
                        slippage_pct = None
                    result.append({
                        "market":       m,
                        "buy_ts":       buy["ts"],
                        "sell_ts":      t["ts"],
                        "buy_price":    buy["price"],
                        "sell_price":   t["price"],
                        "amount":       buy["amount"],
                        "pnl_eur":      round(pnl_eur, 4),
                        "pnl_pct":      round(pnl_pct, 3),
                        "slippage_pct": slippage_pct,
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
            gross_profit = sum(p["pnl_eur"] for p in wins_m)
            gross_loss   = abs(sum(p["pnl_eur"] for p in losses_m))
            profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
            per_market.append({
                "market":        mkt,
                "num_trades":    len(mps),
                "total_pnl":     round(total, 2),
                "win_rate":      round(len(wins_m) / len(mps) * 100, 1) if mps else 0.0,
                "num_wins":      len(wins_m),
                "num_losses":    len(losses_m),
                "avg_pnl_eur":   round(total / len(mps), 2) if mps else 0.0,
                "avg_pnl_pct":   round(sum(p["pnl_pct"] for p in mps) / len(mps), 2) if mps else 0.0,
                "best_trade":    round(max((p["pnl_eur"] for p in mps), default=0), 2),
                "worst_trade":   round(min((p["pnl_eur"] for p in mps), default=0), 2),
                "profit_factor": profit_factor,
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

        # ── Sharpe Ratio + Max Drawdown uit portfolio snapshots ───────────────
        sharpe_ratio        = None
        max_drawdown_pct    = None
        current_drawdown_pct = None
        try:
            import math, statistics as _stats
            snaps  = get_portfolio_snapshots(limit=500)
            totals = [s["total_eur"] for s in snaps]
            if len(totals) >= 5:
                rets = [(totals[i] - totals[i-1]) / totals[i-1]
                        for i in range(1, len(totals)) if totals[i-1] > 0]
                if len(rets) > 1:
                    m_r, s_r = _stats.mean(rets), _stats.stdev(rets)
                    sharpe_ratio = round((m_r / s_r * math.sqrt(365)) if s_r > 0 else 0.0, 3)
                peak_v, max_dd = totals[0], 0.0
                for t in totals:
                    if t > peak_v:
                        peak_v = t
                    dd = (peak_v - t) / peak_v if peak_v > 0 else 0.0
                    max_dd = max(max_dd, dd)
                max_drawdown_pct = round(max_dd * 100, 2)
                cur_peak = max(totals)
                current_drawdown_pct = round(
                    (cur_peak - totals[-1]) / cur_peak * 100 if cur_peak > 0 else 0.0, 2
                )
        except Exception:
            pass

        gross_profit_all = sum(p["pnl_eur"] for p in wins)
        gross_loss_all   = abs(sum(p["pnl_eur"] for p in losses))
        profit_factor_all = round(gross_profit_all / gross_loss_all, 2) if gross_loss_all > 0 else None

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
            "profit_factor":       profit_factor_all,
            "sharpe_ratio":        sharpe_ratio,
            "max_drawdown_pct":    max_drawdown_pct,
            "current_drawdown_pct": current_drawdown_pct,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/benchmark")
def api_benchmark():
    """Vergelijkt bot-prestaties met simpel BTC kopen-en-vasthouden."""
    try:
        from src.database import get_all_paper_trades_asc, get_portfolio_snapshots
        trades = get_all_paper_trades_asc(None)
        if not trades:
            return jsonify({"available": False, "reason": "Nog geen trades"})

        start_date = trades[0]["ts"][:10]
        starting_capital = env_float("PAPER_STARTING_CAPITAL", 1000.0)

        # BTC-prijs op startdatum ophalen uit opgeslagen signalen
        with __import__("src.database", fromlist=["get_conn"]).get_conn() as conn:
            row = conn.execute(
                "SELECT close FROM signals WHERE market='BTC-EUR' AND ts >= ? ORDER BY ts ASC LIMIT 1",
                (start_date,)
            ).fetchone()
        if not row:
            return jsonify({"available": False, "reason": "Geen BTC-EUR signalen vanaf startdatum"})

        btc_start_price = float(row[0])
        btc_amount      = starting_capital / btc_start_price

        # Huidige BTC-prijs
        try:
            client = get_client()
            btc_now = get_ticker_price(client, "BTC-EUR") or btc_start_price
        except Exception:
            btc_now = btc_start_price

        btc_value_now = btc_amount * btc_now
        btc_pnl       = btc_value_now - starting_capital
        btc_pnl_pct   = btc_pnl / starting_capital * 100

        # Huidige bot-waarde uit snapshots
        snaps = get_portfolio_snapshots(limit=1)
        bot_value = snaps[0]["total_eur"] if snaps else starting_capital
        bot_pnl     = bot_value - starting_capital
        bot_pnl_pct = bot_pnl / starting_capital * 100

        return jsonify({
            "available":        True,
            "start_date":       start_date,
            "starting_capital": starting_capital,
            "btc_start_price":  round(btc_start_price, 2),
            "btc_amount":       round(btc_amount, 8),
            "btc_value_now":    round(btc_value_now, 2),
            "btc_pnl":          round(btc_pnl, 2),
            "btc_pnl_pct":      round(btc_pnl_pct, 2),
            "bot_value":        round(bot_value, 2),
            "bot_pnl":          round(bot_pnl, 2),
            "bot_pnl_pct":      round(bot_pnl_pct, 2),
            "alpha":            round(bot_pnl_pct - btc_pnl_pct, 2),
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


@app.route("/export/trades.csv")
def export_trades_csv():
    """Exporteer alle paper trades als CSV voor trade journaling."""
    import csv, io
    trades = get_all_paper_trades_asc(None)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Datum", "Markt", "Side", "Prijs (EUR)", "Hoeveelheid", "Totaal EUR", "Fee EUR", "Reden"])
    for t in trades:
        writer.writerow([
            t.get("ts", ""),
            t.get("market", ""),
            t.get("side", ""),
            t.get("price", ""),
            t.get("amount", ""),
            t.get("eur_total", ""),
            t.get("fee", 0),
            t.get("reason", ""),
        ])
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


def start(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    app.run(host=host, port=port, debug=debug, threaded=True)
