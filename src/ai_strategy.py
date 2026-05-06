"""AI Trading Orchestrator — gespecialiseerde providers in volgorde.

Stap 1  Tactisch    Groq (primair) → Cerebras (backup) — snel, altijd uitgevoerd
Stap 2  Sentiment   Pool: Gemini + Mistral (primair), Groq/Cerebras (fallback)
         Majority-vote; Gemini wint bij staking. Cache: 20 min TTL.
Stap 3  Risico      Lokale manager (deterministisch) — geen AI-call nodig

Providervolgorde per rol hardcoded; aanwezigheid bepaald door geconfigureerde API keys.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from src.database import (
    get_latest_signals, get_cash, get_position, get_paper_trades,
    get_last_buy_ts, get_recent_trade_pairs, get_market_change_24h,
)
from src.env_utils import env_float, env_int

logger = logging.getLogger(__name__)


def ai_enabled() -> bool:
    return os.getenv("AI_STRATEGY_ENABLED", "false").lower() == "true"


# Numerieke score per uitkomst voor gewogen combinatie
_DECISION_SCORE:  dict[str, float] = {"BUY": 1.0, "HOLD": 0.0, "SELL": -1.0}
_SENTIMENT_SCORE: dict[str, float] = {"POSITIVE": 1.0, "NEUTRAL": 0.0, "NEGATIVE": -1.0}

# Cache: {market: (votes_list, monotonic_timestamp)}
# votes_list = [(provider, result_dict), ...]
_sentiment_cache: dict[str, tuple[list, float]] = {}

# ── Provider-rolverdeling (volgorde = prioriteit) ─────────────────────────────
_TACTICAL_CHAIN:       tuple[str, ...] = ("groq", "cerebras", "mistral", "anthropic", "google")
_SENTIMENT_PRIMARY:    tuple[str, ...] = ("google", "mistral")   # altijd bevraagd voor confluence
_SENTIMENT_FALLBACK:   tuple[str, ...] = ("groq", "cerebras")    # bij onvoldoende primaire stemmen

# ── System prompts ────────────────────────────────────────────────────────────

_TACTICAL_PROMPT = """\
You are an expert crypto technical analyst for the Bitvavo exchange.
Evaluate the provided indicator data and return a fast, disciplined trading signal.

Rules:
- BUY  → strong bullish confluence: golden cross + RSI not overbought, price near lower BB with rising MACD
- SELL → strong bearish confluence: death cross, RSI > 70, or significant unrealized loss
- HOLD → mixed/unclear signals, insufficient data, or flat/sideways market; capital preservation takes priority
- Do NOT buy if there is already an open position (unless signal is exceptionally strong)
- Do NOT sell if there is no open position

Volatility rule (IMPORTANT):
- If ATR-14 is below 0.5% of the current price the market is flat/sideways.
  In that case return HOLD with low confidence — do not act on RSI or MA signals alone.
  A flat market produces false signals; wait for a real breakout.

IMPORTANT: respond with ONLY raw JSON — no markdown, no code blocks, no explanation before or after.
Output format (copy exactly, fill in values):
{"decision": "BUY", "confidence": 0.82, "reasoning": "one concise English sentence"}
decision: BUY | SELL | HOLD  —  confidence: 0.0–1.0\
"""

_SENTIMENT_PROMPT = """\
You are a crypto sentiment analyst.
Return ONLY a JSON object. No markdown blocks, no intro, no outro.

{"sentiment": "POSITIVE", "confidence": 0.75, "reasoning": "short explanation"}

Analyze the provided data and respond with the JSON above.
Sentiment must be POSITIVE, NEGATIVE, or NEUTRAL. Confidence 0.0–1.0. Reasoning max 10 words.\
"""


def _orders_executed_today(market: str) -> int:
    from src.database import get_ai_decisions_today
    return get_ai_decisions_today(market)


def _last_trade_minutes_ago(market: str) -> float | None:
    trades = get_paper_trades(market, limit=1)
    if not trades:
        return None
    ts = datetime.fromisoformat(trades[0]["ts"])
    if ts.tzinfo is None:                          # legacy naive timestamps
        from zoneinfo import ZoneInfo
        ts = ts.replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60


def _build_context(market: str, signals: dict, recent_signals: list[dict], fg_str: str = "") -> str:
    pos      = get_position(market)
    cash     = get_cash()
    price    = float(signals.get("close", 0))
    interval = os.getenv("CANDLE_INTERVAL", "1h")

    lines = [
        f"Market: {market}",
        f"Candle timeframe: {interval}",
        f"Current price: €{price:.4f}",
    ]

    change_24h = get_market_change_24h(market)
    if change_24h is not None:
        direction = "▲" if change_24h >= 0 else "▼"
        lines.append(f"24h price change: {direction} {change_24h:+.2f}%")

    if fg_str:
        lines.append(fg_str)

    lines += ["", "=== Technical Indicators ==="]

    if signals.get("sma_20") is not None:
        lines.append(f"SMA 20: €{signals['sma_20']:.4f}")
    if signals.get("sma_50") is not None:
        lines.append(f"SMA 50: €{signals['sma_50']:.4f}")
    if signals.get("rsi_14") is not None:
        rsi   = signals["rsi_14"]
        label = " (OVERBOUGHT ⚠)" if rsi > 70 else (" (OVERSOLD ⚠)" if rsi < 30 else "")
        lines.append(f"RSI 14: {rsi:.2f}{label}")
    if signals.get("macd") is not None:
        hist      = signals.get("macd_hist", 0) or 0
        hist_prev = signals.get("macd_hist_prev", 0) or 0
        hist_dir  = "increasing ↑" if hist > hist_prev else "decreasing ↓"
        lines.append(
            f"MACD: {signals['macd']:.6f}  Signal: {signals['macd_signal']:.6f}  "
            f"Histogram: {hist:.6f} ({'bullish' if hist > 0 else 'bearish'}, {hist_dir})"
        )
    if signals.get("bb_lower") is not None:
        if price < signals["bb_lower"]:
            bb_pos = "BELOW lower band (oversold zone)"
        elif price > signals["bb_upper"]:
            bb_pos = "ABOVE upper band (overbought zone)"
        else:
            bb_mid = (signals["bb_lower"] + signals["bb_upper"]) / 2
            bb_pos = f"inside bands ({'upper half' if price > bb_mid else 'lower half'})"
        lines.append(
            f"Bollinger Bands: €{signals['bb_lower']:.4f} — €{signals['bb_upper']:.4f}  ({bb_pos})"
        )

    ma_cross = signals.get("ma_cross")
    if ma_cross:
        cross_label = "GOLDEN CROSS (bullish)" if ma_cross == "golden_cross" else "DEATH CROSS (bearish)"
        lines.append(f"MA Cross signal: {cross_label}")

    vol     = signals.get("volume")
    vol_avg = signals.get("volume_avg_20")
    if vol is not None and vol_avg and vol_avg > 0:
        vol_ratio = vol / vol_avg
        vol_label = ("HIGH ↑↑" if vol_ratio > 1.5 else
                     "above average ↑" if vol_ratio > 1.1 else
                     "below average ↓" if vol_ratio < 0.9 else "average")
        lines.append(f"Volume: {vol:,.0f}  ({vol_ratio:.1f}× 20-period avg — {vol_label})")

    atr = signals.get("atr_14")
    if atr is not None and price > 0:
        atr_pct   = atr / price * 100
        vol_level = "HIGH volatility ⚠" if atr_pct > 4 else ("elevated" if atr_pct > 2 else "low/normal")
        lines.append(f"ATR-14: €{atr:.4f} ({atr_pct:.2f}% of price — {vol_level})")

    lines += ["", "=== Portfolio State ===",
              f"Available cash: €{cash:.2f}",
              f"Open position: {pos['amount']:.6f} units @ avg €{pos['avg_price']:.4f}"]

    if pos["amount"] > 0 and pos["avg_price"] > 0:
        pnl_eur = (price - pos["avg_price"]) * pos["amount"]
        pnl_pct = (price - pos["avg_price"]) / pos["avg_price"] * 100
        lines.append(f"Unrealized PnL: €{pnl_eur:+.2f} ({pnl_pct:+.2f}%)")

        buy_ts = get_last_buy_ts(market)
        if buy_ts:
            try:
                elapsed      = datetime.utcnow() - datetime.fromisoformat(buy_ts[:19])
                hours        = int(elapsed.total_seconds() / 3600)
                days         = hours // 24
                duration_str = f"{days}d {hours % 24}h" if days > 0 else f"{hours}h"
                lines.append(f"Position open for: {duration_str}")
            except Exception:
                pass

    past_pairs = get_recent_trade_pairs(market, limit=3)
    if past_pairs:
        wins    = sum(1 for p in past_pairs if p["pnl_eur"] > 0)
        total   = len(past_pairs)
        avg_pnl = sum(p["pnl_eur"] for p in past_pairs) / total
        lines += [
            "",
            f"=== Recent Closed Trades ({total} most recent) ===",
            f"Win rate: {wins}/{total}  |  Avg PnL: €{avg_pnl:+.2f}",
        ]
        for p in reversed(past_pairs):
            outcome = "WIN ✓" if p["pnl_eur"] > 0 else "LOSS ✗"
            lines.append(
                f"  {p['sell_ts'][:16]}  {outcome}  "
                f"buy €{p['buy_price']:.4f} → sell €{p['sell_price']:.4f}  "
                f"PnL: €{p['pnl_eur']:+.2f} ({p['pnl_pct']:+.2f}%)"
            )

    from src.database import get_daily_loss
    daily_loss  = get_daily_loss(market)
    daily_limit = env_float("DAILY_LOSS_LIMIT_EUR", 50)
    if daily_loss < 0:
        lines.append(
            f"Daily realized loss: €{daily_loss:.2f} / €{daily_limit:.0f} limit "
            f"({abs(daily_loss) / daily_limit * 100:.0f}% used)"
        )

    if past_pairs:
        loss_streak = 0
        for p in reversed(past_pairs):
            if p["pnl_eur"] < 0:
                loss_streak += 1
            else:
                break
        if loss_streak >= 2:
            lines.append(f"⚠️ LOSS STREAK: {loss_streak} consecutive losses — consider HOLD")

    orders_today = _orders_executed_today(market)
    lines.append(f"AI orders today: {orders_today}/{env_int('AI_MAX_ORDERS_PER_DAY', 3)}")

    if recent_signals:
        lines += ["", "=== Recent Signal History (newest first) ==="]
        for s in recent_signals[:5]:
            lines.append(
                f"  {s['ts'][:16]}  signal={s.get('signal', 'n/a'):<4}  "
                f"price=€{s.get('close', 0):.4f}  RSI={s.get('rsi_14') or 'n/a'}"
            )

    return "\n".join(lines)


# ── Parsers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str, key: str) -> str | None:
    if not text:
        return None

    # 1. Strip markdown code fences (```json ... ``` of ``` ... ```)
    #    Gemini / nieuwere modellen wikkelen output soms hierin ook al vraag je dat niet.
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```", "", cleaned).strip()

    # 2. Brace-depth scan op de schoongemaakte tekst.
    #    Handelt geneste objecten, multi-line JSON en accolades in strings af.
    for i, ch in enumerate(cleaned):
        if ch != '{':
            continue
        depth = 0
        for j in range(i, len(cleaned)):
            if cleaned[j] == '{':
                depth += 1
            elif cleaned[j] == '}':
                depth -= 1
                if depth == 0:
                    candidate = cleaned[i:j + 1]
                    if f'"{key}"' in candidate:
                        return candidate
                    break
    return None


def _parse_decision(text: str) -> dict | None:
    raw = _extract_json(text, "decision")
    if not raw:
        logger.debug("_parse_decision: geen JSON gevonden in: %.300s", text)
        return None
    try:
        data      = json.loads(raw)
        decision  = str(data.get("decision", "HOLD")).upper().strip()
        if decision not in ("BUY", "SELL", "HOLD"):
            decision = "HOLD"
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        reasoning  = str(data.get("reasoning", ""))
        return {"decision": decision, "confidence": confidence, "reasoning": reasoning}
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.debug("_parse_decision: JSON-parse fout (%s) in: %.300s", exc, raw)
        return None


def _parse_sentiment(text: str) -> dict | None:
    """Parse Gemini sentiment — probeert JSON eerst, valt terug op regex bij truncatie."""
    if not text:
        return None

    # ── Pad A: volledige JSON-extractie ─────────────────────────────────────
    raw = _extract_json(text, "sentiment")
    if raw:
        try:
            data     = json.loads(raw)
            sent_raw = data.get("sentiment", "NEUTRAL")
            if isinstance(sent_raw, (int, float)):
                s = float(sent_raw)
                sentiment = "POSITIVE" if s > 0.2 else ("NEGATIVE" if s < -0.2 else "NEUTRAL")
            else:
                s = str(sent_raw).upper().strip()
                sentiment = "POSITIVE" if "POS" in s else ("NEGATIVE" if "NEG" in s else "NEUTRAL")
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
            reasoning  = str(data.get("reasoning", data.get("reason", data.get("analysis", ""))))
            return {"sentiment": sentiment, "confidence": confidence, "reasoning": reasoning}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.debug("_parse_sentiment JSON-parse fout (%s) — valt terug op regex", exc)

    cleaned = re.sub(r"```(?:json)?\s*|```", "", text).strip()

    # ── Pad B: regex op (afgebroken) JSON — sluitende " niet vereist ─────────
    sent_m = re.search(r'"sentiment"\s*:\s*"([A-Za-z]+)', cleaned, re.IGNORECASE)
    conf_m = re.search(r'"confidence"\s*:\s*([0-9.]+)', cleaned)
    if sent_m:
        s = sent_m.group(1).upper().strip()
        sentiment  = "POSITIVE" if "POS" in s else ("NEGATIVE" if "NEG" in s else "NEUTRAL")
        confidence = max(0.0, min(1.0, float(conf_m.group(1)))) if conf_m else 0.5
        reason_m   = re.search(r'"(?:reasoning|reason)"\s*:\s*"([^"]*)', cleaned)
        reasoning  = reason_m.group(1) if reason_m else "(afgebroken)"
        logger.debug("_parse_sentiment: Pad B (regex) voor '%.80s'", cleaned[:80])
        return {"sentiment": sentiment, "confidence": confidence, "reasoning": reasoning}

    # ── Pad C: keyword-scan voor vrije tekst (JSON-instructie genegeerd) ─────
    upper = cleaned.upper()
    if any(w in upper for w in ("BULLISH", "POSITIVE", "UPTREND", "UPWARD", "STRONG BUY",
                                "BUY", "MOON", "STRENGTH")):
        kw_sent = "POSITIVE"
    elif any(w in upper for w in ("BEARISH", "NEGATIVE", "DOWNTREND", "BEAR", "STRONG SELL",
                                  "SELL", "WEAKNESS", "DUMP")):
        kw_sent = "NEGATIVE"
    elif any(w in upper for w in ("NEUTRAL", "SIDEWAYS", "MIXED", "FLAT",
                                   "UNCERTAIN", "CONSOLIDAT", "RANGE", "LOW VOLUME",
                                   "ALIGN", "INDECIS", "WAIT")):
        kw_sent = "NEUTRAL"
    elif cleaned:
        # Pad D: niet-lege response maar geen enkel herkenbaar signaal →
        # val terug op NEUTRAL 0.30 zodat de eindscore onveranderd blijft (+0.00)
        logger.debug("_parse_sentiment: Pad D (default NEUTRAL) voor '%.80s'", cleaned[:80])
        return {"sentiment": "NEUTRAL", "confidence": 0.30,
                "reasoning": f"(geen signaal: {cleaned[:60]})"}
    else:
        logger.warning("_parse_sentiment: lege response")
        return None
    logger.debug("_parse_sentiment: Pad C (keyword) voor '%.80s'", cleaned[:80])
    return {"sentiment": kw_sent, "confidence": 0.40,
            "reasoning": f"(keyword: {cleaned[:60]})"}



def _tech_confluence(signals: dict, price: float) -> tuple[int, str]:
    """
    Lokale technische confluentiesscore (0–5) zonder API-call.

    Telt het aantal gealignde indicatoren en bepaalt de dominante richting.
    Returns: (score, direction) — direction is "bullish" | "bearish" | "mixed".
    """
    bullish = 0
    bearish = 0

    rsi = signals.get("rsi_14")
    if rsi is not None:
        if float(rsi) < 35:
            bullish += 1
        elif float(rsi) > 65:
            bearish += 1

    if signals.get("ma_cross") == "golden_cross":
        bullish += 1
    elif signals.get("ma_cross") == "death_cross":
        bearish += 1

    sma_20 = signals.get("sma_20")
    sma_50 = signals.get("sma_50")
    if sma_20 and sma_50 and float(sma_50) > 0:
        if float(sma_20) > float(sma_50):
            bullish += 1
        else:
            bearish += 1

    vol     = signals.get("volume")
    vol_avg = signals.get("volume_avg_20")
    if vol and vol_avg and float(vol_avg) > 0 and float(vol) / float(vol_avg) > 1.2:
        if bullish >= bearish:
            bullish += 1
        else:
            bearish += 1

    hist      = float(signals.get("macd_hist") or 0)
    hist_prev = float(signals.get("macd_hist_prev") or 0)
    if hist > 0 and hist > hist_prev:
        bullish += 1
    elif hist < 0 and hist < hist_prev:
        bearish += 1

    direction = "bullish" if bullish > bearish else ("bearish" if bearish > bullish else "mixed")
    return max(bullish, bearish), direction


def _local_risk_check(
    market: str,
    signals: dict,
    price: float,
    potential_action: str,
    combined_score: float,
) -> tuple[bool, float, str]:
    """
    Deterministisch risicobeheer zonder API-call.
    Controleert dezelfde regels als de voormalige AI-risicomanager.
    Returns: (approved, confidence, reasoning)
    """
    from src.database import get_daily_loss

    # Verliesreeks ≥ 3 opeenvolgende trades → weiger
    past_pairs = get_recent_trade_pairs(market, limit=3)
    if past_pairs:
        loss_streak = 0
        for p in reversed(past_pairs):
            if p["pnl_eur"] < 0:
                loss_streak += 1
            else:
                break
        if loss_streak >= 3:
            return False, 0.9, f"Verliesreeks: {loss_streak} opeenvolgende verliezen"

    # Daglimiet > 80% verbruikt → weiger
    daily_loss  = get_daily_loss(market)
    daily_limit = env_float("DAILY_LOSS_LIMIT_EUR", 50)
    if daily_loss < 0 and daily_limit > 0:
        used_pct = abs(daily_loss) / daily_limit
        if used_pct >= 0.8:
            return False, 0.85, f"Daglimiet {used_pct:.0%} verbruikt (€{abs(daily_loss):.2f}/€{daily_limit:.0f})"

    # Extreme volatiliteit ATR > 8% → weiger
    atr = signals.get("atr_14")
    if atr is not None and price > 0:
        atr_pct = float(atr) / price * 100
        if atr_pct > 8:
            return False, 0.75, f"Extreme volatiliteit: ATR {atr_pct:.1f}% > 8%"

    # BUY terwijl open positie > 15% in verlies → weiger
    if potential_action == "BUY":
        pos = get_position(market)
        if pos["amount"] > 0 and float(pos.get("avg_price", 0)) > 0 and price > 0:
            pnl_pct = (price - float(pos["avg_price"])) / float(pos["avg_price"]) * 100
            if pnl_pct < -15:
                return False, 0.8, f"Open positie in diep verlies ({pnl_pct:.1f}%)"

    return True, min(abs(combined_score), 1.0), "Risicocheck: geen bezwaren"


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _combine_sentiment_votes(votes: list[tuple[str, dict]]) -> dict | None:
    """Majority-vote over sentiment-stemmen. Gemini wint bij staking."""
    if not votes:
        return None
    if len(votes) == 1:
        _, result = votes[0]
        return result

    counts: dict[str, int] = {"POSITIVE": 0, "NEGATIVE": 0, "NEUTRAL": 0}
    for _, r in votes:
        counts[r["sentiment"]] += 1

    max_count = max(counts.values())
    winners = [s for s, c in counts.items() if c == max_count]

    if len(winners) == 1:
        winner = winners[0]
    else:
        # Staking: Google (Gemini) wint als casting vote
        google_sent = next((r["sentiment"] for p, r in votes if p == "google"), None)
        winner = google_sent if google_sent else winners[0]

    winning_votes = [(p, r) for p, r in votes if r["sentiment"] == winner]
    avg_conf = sum(r["confidence"] for _, r in winning_votes) / len(winning_votes)
    # Klein bonusbedrag wanneer meerdere providers het eens zijn
    conf = min(1.0, avg_conf + 0.05 * (len(winning_votes) - 1))

    reasoning = " | ".join(f"[{p}] {r['reasoning']}" for p, r in winning_votes)
    all_votes  = ", ".join(f"{p}:{r['sentiment']}" for p, r in votes)
    logger.info("Sentiment pool: %d/%d eens over %s (conf=%.2f) [%s]",
                len(winning_votes), len(votes), winner, conf, all_votes)

    return {"sentiment": winner, "confidence": conf, "reasoning": reasoning}


def _assign_roles(providers: list[tuple[str, str]]) -> dict[str, str]:
    """
    Wijst tactische en risico-rollen toe.

    Sentiment is een pool (zie _SENTIMENT_PRIMARY/_SENTIMENT_FALLBACK) en krijgt
    geen expliciete rol. Mogelijke rollen: "tactical" | "risk"
    """
    pdict = dict(providers)
    roles: dict[str, str] = {}

    # Tactisch: eerste beschikbare in _TACTICAL_CHAIN
    for p in _TACTICAL_CHAIN:
        if p in pdict:
            roles[p] = "tactical"
            break

    # Risico: Anthropic alleen als het NIET tactisch is
    if "anthropic" in pdict and roles.get("anthropic") != "tactical":
        roles["anthropic"] = "risk"

    return roles


def ai_evaluate(market: str, signals: dict) -> tuple[str, float, str]:
    """
    Trading Orchestrator: drie AI-providers in volgorde van snelheid en kosten.

    Retourneert: (decision, confidence, reasoning)
    """
    min_confidence     = env_float("AI_MIN_CONFIDENCE", 0.7)
    max_orders_per_day = env_int("AI_MAX_ORDERS_PER_DAY", 3)
    cooldown_minutes   = env_int("AI_COOLDOWN_MINUTES", 60)
    score_threshold    = env_float("AI_SCORE_THRESHOLD", 0.5)

    if not ai_enabled():
        return "HOLD", 0.0, "AI strategie uitgeschakeld"

    if _orders_executed_today(market) >= max_orders_per_day:
        logger.info("[%s] AI: dagelijks maximum bereikt (%d)", market, max_orders_per_day)
        return "HOLD", 0.0, f"Max {max_orders_per_day} orders per dag bereikt"

    minutes_ago = _last_trade_minutes_ago(market)
    if minutes_ago is not None and minutes_ago < cooldown_minutes:
        remaining = int(cooldown_minutes - minutes_ago)
        return "HOLD", 0.0, f"Cooldown: wacht nog {remaining} minuten"

    # ── Lokale ATR pre-filter — VÓÓR elke API-call ────────────────────────────
    price       = float(signals.get("close") or 0)
    atr         = signals.get("atr_14")
    sensitivity = env_float("ATR_SENSITIVITY", 0.8)

    if atr is not None and price > 0:
        atr_pct = float(atr) / price * 100

        avg_atr_24h = signals.get("avg_atr_24h")
        if sensitivity > 0 and avg_atr_24h is not None and avg_atr_24h > 0:
            # Route B: dynamische drempel — vergelijk met 24-candle gemiddelde
            avg_atr_pct   = float(avg_atr_24h) / price * 100
            dyn_threshold = avg_atr_pct * sensitivity
            if atr_pct < dyn_threshold:
                logger.info(
                    "[%s] Platte markt — ATR %.2f%% < %.2f%% (%.0f%% van 24h-gem %.2f%%) — HOLD",
                    market, atr_pct, dyn_threshold, sensitivity * 100, avg_atr_pct,
                )
                return "HOLD", 0.0, (
                    f"Platte markt (Relatief: ATR {atr_pct:.2f}% < {dyn_threshold:.2f}%)"
                )
        else:
            # Route A: statische drempel (fallback of ATR_SENSITIVITY=0)
            atr_threshold = env_float("ATR_FLAT_THRESHOLD", 0.5)
            if atr_pct < atr_threshold:
                logger.info(
                    "[%s] Platte markt (ATR %.2f%% < %.2f%%) — HOLD (lokaal, geen API)",
                    market, atr_pct, atr_threshold,
                )
                return "HOLD", 0.0, f"Platte markt (Lokaal gefilterd, ATR {atr_pct:.2f}%)"

    # ── Trendfilter als vroege gatekeeper (vóór alle API-calls) ─────────────
    sma_200 = signals.get("sma_200")
    trend_bearish = (
        sma_200 is not None and price > 0 and float(price) < float(sma_200)
        and os.getenv("TREND_FILTER_ENABLED", "1") not in ("0", "false", "False")
    )
    if trend_bearish:
        _early_pos = get_position(market)
        if _early_pos["amount"] <= 0:
            logger.info(
                "[%s] Trendfilter (vroeg): prijs €%.4f onder SMA200 €%.4f — geen positie, HOLD (0 API-calls)",
                market, price, float(sma_200),
            )
            return "HOLD", 0.0, (
                f"Trendfilter: prijs €{price:.4f} onder SMA200 €{float(sma_200):.4f}"
            )
        logger.info(
            "[%s] Trendfilter actief (bearish, positie open): sentiment overgeslagen",
            market,
        )

    # ── Technische confluentiesscore — vroege exit + modelkeuze ──────────────
    confluence, conf_dir = _tech_confluence(signals, price)
    min_conf_score  = env_int("MIN_CONFLUENCE_SCORE", 2)
    high_conf_score = env_int("HIGH_CONFLUENCE_SCORE", 4)
    _eff_min = min_conf_score

    # Open positie met verlies en bearish signaal → drempel naar 1 (SELL mogelijk nodig)
    if confluence >= 1 and conf_dir == "bearish":
        _pos_conf = _early_pos if trend_bearish else get_position(market)
        if _pos_conf["amount"] > 0 and float(_pos_conf.get("avg_price", 0)) > 0:
            _pnl_conf = (price - float(_pos_conf["avg_price"])) / float(_pos_conf["avg_price"]) * 100
            if _pnl_conf < -3:
                _eff_min = 1

    if confluence < _eff_min:
        logger.info(
            "[%s] Confluence %d/%d (%s) — te laag voor AI (geen API-calls)",
            market, confluence, _eff_min, conf_dir,
        )
        return "HOLD", 0.0, f"Technische confluence {confluence} ({conf_dir}) te laag"

    is_strong_signal = confluence >= high_conf_score
    logger.info("[%s] Confluence: %d (%s) → %s modellen",
                market, confluence, conf_dir, "zwaar" if is_strong_signal else "licht")

    recent_signals = get_latest_signals(market, limit=3)

    from src.sentiment import get_fear_greed, fmt_fear_greed
    fg_str  = fmt_fear_greed(get_fear_greed())
    context = _build_context(market, signals, recent_signals, fg_str)
    prompt  = "Analyze the following market data:\n\n" + context

    try:
        from src.ai_provider import get_configured_providers, complete_for
        providers = get_configured_providers()
        if not providers:
            return "HOLD", 0.0, "Geen AI provider geconfigureerd of ingeschakeld"

        pdict = dict(providers)
        roles = _assign_roles(providers)

        # ── Stap 1: Tactische Verkenner — fallback-keten ─────────────────────
        tactical_prov:   str | None  = None
        tactical_result: dict | None = None
        tactical_score   = 0.0

        for cand in _TACTICAL_CHAIN:
            if cand not in pdict:
                continue
            try:
                text   = complete_for(cand, pdict[cand], _TACTICAL_PROMPT, prompt, max_tokens=160)
                logger.debug("[%s] %s raw: %.200s", market, cand, text)
                parsed = _parse_decision(text)
                if parsed:
                    tactical_prov   = cand
                    tactical_result = parsed
                    tactical_score  = _DECISION_SCORE[parsed["decision"]] * parsed["confidence"]
                    logger.info("[%s] %s (tactisch): %s %.0f%% score=%+.2f",
                                market, cand, parsed["decision"],
                                parsed["confidence"] * 100, tactical_score)
                    break
                logger.warning("[%s] %s: kon tactisch besluit niet parsen", market, cand)
            except Exception as exc:
                logger.warning("[%s] %s (tactisch) fout: %s — probeer backup", market, cand, exc)

        if tactical_result is None:
            return "HOLD", 0.0, "Geen tactisch analyse resultaat beschikbaar"

        # Snelle HOLD als geen sentiment-providers beschikbaar zijn
        any_sentiment = any(p in pdict for p in _SENTIMENT_PRIMARY + _SENTIMENT_FALLBACK)
        if not any_sentiment and abs(tactical_score) < score_threshold:
            return "HOLD", abs(tactical_score), (
                f"Score {tactical_score:+.2f} onder drempel {score_threshold:.1f} — "
                f"{tactical_result['reasoning']}"
            )

        # ── Stap 2: Sentiment Pool — met cache, overgeslagen bij bearish trend ─
        sentiment_result: dict | None = None
        sentiment_score  = 0.0

        if not trend_bearish:
            cache_ttl    = env_float("SENTIMENT_CACHE_MINUTES", 20.0) * 60
            now_mono     = time.monotonic()
            cached_entry = _sentiment_cache.get(market)

            if cached_entry and (now_mono - cached_entry[1]) < cache_ttl:
                cached_votes = cached_entry[0]
                sentiment_result = _combine_sentiment_votes(cached_votes)
                if sentiment_result:
                    sentiment_score = (_SENTIMENT_SCORE[sentiment_result["sentiment"]]
                                       * sentiment_result["confidence"])
                    logger.info("[%s] Sentiment pool (cache %ds oud): %s %.0f%%",
                                market, int(now_mono - cached_entry[1]),
                                sentiment_result["sentiment"],
                                sentiment_result["confidence"] * 100)
            else:
                votes: list[tuple[str, dict]] = []
                queried: set[str] = set()

                # Primaire pool: Gemini + Mistral
                for sent_prov in _SENTIMENT_PRIMARY:
                    if sent_prov not in pdict:
                        continue
                    queried.add(sent_prov)
                    try:
                        text   = complete_for(sent_prov, pdict[sent_prov],
                                              _SENTIMENT_PROMPT, prompt, max_tokens=80)
                        logger.debug("[%s] %s raw: %.200s", market, sent_prov, text)
                        parsed = _parse_sentiment(text)
                        if parsed:
                            votes.append((sent_prov, parsed))
                            logger.info("[%s] %s (sentiment): %s %.0f%%",
                                        market, sent_prov, parsed["sentiment"],
                                        parsed["confidence"] * 100)
                        else:
                            logger.warning("[%s] %s: kon sentiment niet parsen", market, sent_prov)
                    except Exception as exc:
                        logger.warning("[%s] %s (sentiment) fout: %s", market, sent_prov, exc)

                # Fallback pool: Groq / Cerebras (als primaire onvoldoende)
                if len(votes) < 2:
                    for fb_prov in _SENTIMENT_FALLBACK:
                        if fb_prov not in pdict or fb_prov in queried:
                            continue
                        queried.add(fb_prov)
                        try:
                            text   = complete_for(fb_prov, pdict[fb_prov],
                                                  _SENTIMENT_PROMPT, prompt, max_tokens=80)
                            logger.debug("[%s] %s raw: %.200s", market, fb_prov, text)
                            parsed = _parse_sentiment(text)
                            if parsed:
                                votes.append((fb_prov, parsed))
                                logger.info("[%s] %s (sentiment fallback): %s %.0f%%",
                                            market, fb_prov, parsed["sentiment"],
                                            parsed["confidence"] * 100)
                        except Exception as exc:
                            logger.warning("[%s] %s (sentiment fallback) fout: %s", market, fb_prov, exc)
                        if len(votes) >= 2:
                            break

                if votes:
                    _sentiment_cache[market] = (votes, now_mono)
                    sentiment_result = _combine_sentiment_votes(votes)
                    if sentiment_result:
                        sentiment_score = (_SENTIMENT_SCORE[sentiment_result["sentiment"]]
                                           * sentiment_result["confidence"])

        # ── Stap 3: Gewogen gecombineerde score ──────────────────────────────
        if sentiment_result is not None:
            combined_score = tactical_score * 0.7 + sentiment_score * 0.3
            logger.info("[%s] Score: tactisch=%+.2f × 70%% + sentiment=%+.2f × 30%% = %+.2f",
                        market, tactical_score, sentiment_score, combined_score)
        else:
            combined_score = tactical_score
            logger.info("[%s] Score: tactisch=%+.2f (geen sentiment)", market, combined_score)

        if abs(combined_score) < score_threshold:
            parts = [f"[{tactical_prov}] {tactical_result['reasoning']}"]
            if sentiment_result:
                parts.append(sentiment_result['reasoning'])
            return "HOLD", abs(combined_score), (
                f"Score {combined_score:+.2f} onder drempel {score_threshold:.1f} — "
                + " | ".join(parts)
            )

        potential_action = "BUY" if combined_score > 0 else "SELL"

        # ── Trendfilter veto ─────────────────────────────────────────────────
        if potential_action == "BUY" and trend_bearish:
            logger.info("[%s] BUY geblokkeerd: prijs €%.4f onder SMA200 €%.4f (bearish trend)",
                        market, price, sma_200)
            return "HOLD", abs(combined_score), (
                f"Trendfilter: prijs onder SMA200 (bearish) | "
                + f"[{tactical_prov}] {tactical_result['reasoning']}"
            )

        # ── Sentiment hard veto: NEGATIVE blokkeert BUY ──────────────────────
        veto_conf = env_float("SENTIMENT_VETO_CONF", 0.6)
        if (potential_action == "BUY"
                and sentiment_result is not None
                and sentiment_result["sentiment"] == "NEGATIVE"
                and sentiment_result["confidence"] >= veto_conf):
            logger.info("[%s] BUY geblokkeerd: sentiment NEGATIVE %.0f%% (veto)",
                        market, sentiment_result["confidence"] * 100)
            return "HOLD", abs(combined_score), (
                f"Sentiment veto: NEGATIVE {sentiment_result['confidence']:.0%} | "
                + f"[{tactical_prov}] {tactical_result['reasoning']}"
            )

        # ── Stap 4: Risicomanager (alleen bij Anthropic + concrete trade) ───────
        risk_prov = next((p for p, r in roles.items() if r == "risk"), None)
        final_confidence = min(abs(combined_score), 1.0)
        base_reasoning_parts = [f"[{tactical_prov}] {tactical_result['reasoning']}"]
        if sentiment_result:
            base_reasoning_parts.append(sentiment_result['reasoning'])

        risk_approved, risk_confidence, risk_reasoning = _local_risk_check(
            market, signals, price, potential_action, combined_score
        )
        logger.info("[%s] Risico (lokaal): %s %.0f%% — %s",
                    market, "OK" if risk_approved else "AFGEWEZEN",
                    risk_confidence * 100, risk_reasoning)
        if not risk_approved:
            return "HOLD", risk_confidence, (
                f"Risicobeheer: {risk_reasoning} | " + " | ".join(base_reasoning_parts)
            )
        final_confidence = (abs(combined_score) + risk_confidence) / 2
        base_reasoning_parts.append(f"[risico] {risk_reasoning}")

        # Finale confidence check
        if final_confidence < min_confidence:
            logger.info("[%s] Afgewezen: confidence %.0f%% < minimum %.0f%%",
                        market, final_confidence * 100, min_confidence * 100)
            return "HOLD", final_confidence, (
                f"Confidence {final_confidence:.0%} onder minimum {min_confidence:.0%}: "
                + " | ".join(base_reasoning_parts)
            )

        reasoning = " | ".join(base_reasoning_parts)
        logger.info("[%s] Besluit: %s (%.0f%%) — %s", market, potential_action, final_confidence * 100, reasoning)
        return potential_action, final_confidence, reasoning

    except EnvironmentError as exc:
        logger.error("AI configuratiefout: %s", exc)
        return "HOLD", 0.0, str(exc)
    except Exception as exc:
        logger.error("[%s] AI onverwachte fout: %s", market, exc, exc_info=True)
        return "HOLD", 0.0, f"AI fout: {exc}"
