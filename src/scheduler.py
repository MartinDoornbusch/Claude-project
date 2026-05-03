"""Scheduler — draait de strategie periodiek via APScheduler."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.bitvavo_client import get_client
from src.candles import get_candles, latest_signals, add_indicators, get_atr_fraction, get_risk_fraction
from src.database import (
    init_db, save_ai_decision, save_signal, get_enabled_markets,
    save_portfolio_snapshot, get_trading_paused, set_trading_paused,
    get_latest_portfolio_total, get_cash, mark_ai_decision_executed,
    get_portfolio_peak, get_pending_accuracy_decisions, save_ai_accuracy,
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
    interval      = os.getenv("CANDLE_INTERVAL", "1h")
    check_minutes = env_int("CHECK_INTERVAL_MINUTES", 60)
    vol_sizing    = os.getenv("VOL_SIZING_ENABLED", "false").lower() == "true"
    corr_check    = os.getenv("CORR_CHECK_ENABLED", "false").lower() == "true"

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

    # Circuit breaker: automatische pauze bij te groot portfolio drawdown
    from src.env_utils import env_float_opt
    cb_pct = env_float_opt("CIRCUIT_BREAKER_PCT")
    if cb_pct and cb_pct > 0 and not paused:
        peak = get_portfolio_peak()
        if peak > 0:
            dd_pct = (peak - portfolio_total) / peak * 100
            if dd_pct >= cb_pct:
                set_trading_paused(True)
                paused = True
                logger.warning(
                    "CIRCUIT BREAKER geactiveerd: drawdown %.1f%% ≥ %.1f%% — trading automatisch gepauzeerd",
                    dd_pct, cb_pct,
                )
                try:
                    from src.notifier import notify_error
                    notify_error("PORTFOLIO", f"Circuit breaker: drawdown {dd_pct:.1f}% ≥ {cb_pct:.1f}%")
                except Exception:
                    pass

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

            # Lage-volume filter: sla markten over met te weinig 24h-liquiditeit
            min_vol_eur = env_float("MIN_VOLUME_EUR", 0.0)
            if min_vol_eur > 0:
                vol_avg = sig.get("volume_avg_20") or 0
                vol_eur_per_candle = vol_avg * current_price
                if vol_eur_per_candle < min_vol_eur:
                    logger.info(
                        "[%s] Volume te laag (€%.0f/candle < €%.0f) — overgeslagen",
                        market, vol_eur_per_candle, min_vol_eur,
                    )
                    continue

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

            ai_decision_id: int | None = None
            if ai_enabled():
                decision, confidence, reasoning = ai_evaluate(market, sig)
                # Sla op als nog-niet-uitgevoerd; wordt bijgewerkt ná echte fill
                ai_decision_id = save_ai_decision(
                    market, decision, confidence, reasoning,
                    executed=False, entry_price=current_price,
                )
                signal = decision
                reason = f"AI ({confidence:.0%}): {reasoning}"
            else:
                signal = evaluate(market, interval, df, client=client)
                reason = "MA crossover / RSI"

            # Throttle: voorkom Groq/Gemini 429-fouten bij veel markten
            ai_delay = env_float("AI_CALL_DELAY_SECONDS", 1.0)
            if ai_delay > 0:
                time.sleep(ai_delay)

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
                        result = execute_buy(client, market, current_price, reason=reason,
                                             fraction=fraction)
                        if result and ai_decision_id:
                            mark_ai_decision_executed(ai_decision_id)
                elif signal == "SELL":
                    result = execute_sell(client, market, current_price, reason=reason)
                    if result and ai_decision_id:
                        mark_ai_decision_executed(ai_decision_id)

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

    # Sentiment accuracy evaluatie: beoordeel AI-beslissingen die oud genoeg zijn
    if ai_enabled() and market_prices:
        try:
            horizon_h = env_float("AI_ACCURACY_HORIZON_HOURS", 8.0)
            for dec in get_pending_accuracy_decisions(horizon_h):
                mkt = dec["market"]
                if mkt not in market_prices:
                    continue
                entry  = dec["entry_price"] or 0.0
                if entry <= 0:
                    continue
                ev_price = market_prices[mkt]
                pnl_pct  = (ev_price - entry) / entry * 100
                if dec["decision"] == "BUY":
                    outcome = "WIN" if pnl_pct > 0.5 else ("LOSS" if pnl_pct < -0.5 else "NEUTRAL")
                else:  # SELL
                    outcome = "WIN" if pnl_pct < -0.5 else ("LOSS" if pnl_pct > 0.5 else "NEUTRAL")
                save_ai_accuracy(
                    dec["id"], mkt, dec["decision"], dec["confidence"],
                    entry, ev_price, pnl_pct, outcome, horizon_h,
                )
                logger.info(
                    "[%s] AI accuracy: %s @ €%.4f → €%.4f (%.1f%%) = %s",
                    mkt, dec["decision"], entry, ev_price, pnl_pct, outcome,
                )
        except Exception as exc:
            logger.warning("Accuracy evaluatie mislukt: %s", exc)


class _AmsFormatter(logging.Formatter):
    """Log-formatter die Amsterdam-lokale tijd toont in plaats van UTC."""
    from zoneinfo import ZoneInfo as _ZI
    _tz = _ZI("Europe/Amsterdam")

    def formatTime(self, record, datefmt=None):
        import datetime as _dt
        ct = _dt.datetime.fromtimestamp(record.created, tz=self._tz)
        return ct.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


from collections import deque

_log_buffer: deque = deque(maxlen=500)


class _BufferHandler(logging.Handler):
    """Slaat de laatste 500 logregels op in geheugen voor de web-UI."""
    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append({
            "ts":      self.formatter.formatTime(record) if self.formatter else "",
            "level":   record.levelname,
            "name":    record.name,
            "message": record.getMessage(),
        })


def get_log_buffer() -> list[dict]:
    return list(_log_buffer)


def start() -> None:
    fmt = _AmsFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    buf_handler = _BufferHandler()
    buf_handler.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=[stream_handler, buf_handler])

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
