"""AI Trading Orchestrator — drie gespecialiseerde providers in volgorde.

Stap 1  Groq        Tactische Verkenner  — snelle TA, altijd uitgevoerd
Stap 2  Gemini      Sentiment Analist    — nieuws/marktsfeer, alleen bij potentieel signaal
Stap 3  Anthropic   Risicomanager        — finale validatie, alleen bij concrete trade

Rolverdeling is automatisch op basis van geconfigureerde providers:
- Groq  → tactisch (technisch)
- Google → sentiment (alleen als er ook een tactisch provider is)
- Anthropic → risicomanager (alleen als Groq tactisch is; anders zelf tactisch)
"""

from __future__ import annotations

import json
import logging
import os
import re
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
You are a crypto market sentiment analyst for the Bitvavo exchange.
Assess the current market mood from the price action, volume, and macro context provided.

The data you receive comes from the candle timeframe shown in the context header.
Use that timeframe to calibrate your interpretation:
- Short timeframes (1m–15m): react to recent momentum bursts and micro-sentiment
- Medium timeframes (1h–4h): focus on intra-day trend and volume patterns
- Daily/weekly: evaluate macro trend health and fear/greed context

Guidelines:
- POSITIVE → clear uptrend for this timeframe, healthy volume, fear/greed in neutral-to-greed zone
- NEGATIVE → downtrend, extreme greed (correction risk), panic-sell patterns, or deteriorating momentum
- NEUTRAL  → mixed or insufficient signals

IMPORTANT: respond with ONLY raw JSON — no markdown, no code blocks, no explanation before or after.
Output format (copy exactly, fill in values):
{"sentiment": "POSITIVE", "confidence": 0.75, "reasoning": "one concise English sentence"}
sentiment: POSITIVE | NEGATIVE | NEUTRAL  —  confidence: 0.0–1.0\
"""

_RISK_PROMPT = """\
You are a risk manager for a crypto trading bot on the Bitvavo exchange.
You receive a proposed trade action and all available analysis. Decide if the trade is safe to execute.

Approve if:
- Proposed action aligns with market data and portfolio state
- No excessive loss streak (fewer than 3 consecutive losses)
- Daily loss limit is not nearly exhausted (< 80% used)
- Combined AI confidence is convincing

Reject if:
- 3+ consecutive losing trades (bot is in a bad streak)
- Daily loss limit > 80% used
- Extreme volatility makes the outcome unpredictable
- Proposed action clearly contradicts the market data
- Open position already shows a deep unrealized loss (> 15%)

IMPORTANT: respond with ONLY raw JSON — no markdown, no code blocks, no explanation before or after.
Output format (copy exactly, fill in values):
{"approved": true, "confidence": 0.88, "reasoning": "one concise English sentence"}
approved: true | false  —  confidence: 0.0–1.0\
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

    past_pairs = get_recent_trade_pairs(market, limit=5)
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
        for p in reversed(past_pairs[-5:]):
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
    raw = _extract_json(text, "sentiment")
    if not raw:
        logger.warning("_parse_sentiment: geen JSON gevonden — raw: %.400s", text)
        return None
    try:
        data = json.loads(raw)
        sent_raw = data.get("sentiment", "NEUTRAL")
        if isinstance(sent_raw, (int, float)):
            # Numerieke score: >0.2 = POSITIVE, <-0.2 = NEGATIVE
            s = float(sent_raw)
            sentiment = "POSITIVE" if s > 0.2 else ("NEGATIVE" if s < -0.2 else "NEUTRAL")
        else:
            sentiment = str(sent_raw).upper().strip()
            if "POS" in sentiment:
                sentiment = "POSITIVE"
            elif "NEG" in sentiment:
                sentiment = "NEGATIVE"
            else:
                sentiment = "NEUTRAL"
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        reasoning  = str(data.get("reasoning", data.get("reason", data.get("analysis", ""))))
        return {"sentiment": sentiment, "confidence": confidence, "reasoning": reasoning}
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("_parse_sentiment: JSON-parse fout (%s) — raw: %.400s", exc, raw)
        return None


def _parse_risk(text: str) -> dict | None:
    raw = _extract_json(text, "approved")
    if not raw:
        logger.debug("_parse_risk: geen JSON gevonden in: %.300s", text)
        return None
    try:
        data       = json.loads(raw)
        approved   = bool(data.get("approved", False))
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        reasoning  = str(data.get("reasoning", ""))
        return {"approved": approved, "confidence": confidence, "reasoning": reasoning}
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.debug("_parse_risk: JSON-parse fout (%s) in: %.300s", exc, raw)
        return None


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _assign_roles(providers: list[tuple[str, str]]) -> dict[str, str]:
    """
    Wijst rollen toe op basis van beschikbare providers.

    Resultaat: {"groq": "tactical", "google": "sentiment", "anthropic": "risk"}
    Mogelijke rollen: "tactical" | "sentiment" | "risk" | "tactical_only"
    """
    pdict = dict(providers)
    roles: dict[str, str] = {}

    has_groq      = "groq" in pdict
    has_google    = "google" in pdict
    has_anthropic = "anthropic" in pdict

    if has_groq:
        roles["groq"] = "tactical"
        if has_anthropic:
            roles["anthropic"] = "risk"
        if has_google:
            roles["google"] = "sentiment"
    elif has_anthropic:
        # Geen Groq → Anthropic speelt zowel tactisch als risico niet dubbel;
        # gebruik het als enige tactische analyst.
        roles["anthropic"] = "tactical_only"
        if has_google:
            roles["google"] = "sentiment"
    elif has_google:
        # Alleen Google → tactisch fallback
        roles["google"] = "tactical_only"

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

    recent_signals = get_latest_signals(market, limit=5)

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

        # ── Stap 1: Tactische Verkenner (Groq of fallback) ───────────────────
        tactical_prov = next(
            (p for p, r in roles.items() if r in ("tactical", "tactical_only")), None
        )
        tactical_result: dict | None = None
        tactical_score = 0.0

        if tactical_prov:
            try:
                text = complete_for(tactical_prov, pdict[tactical_prov],
                                    _TACTICAL_PROMPT, prompt, max_tokens=512)
                logger.debug("[%s] %s raw: %.200s", market, tactical_prov, text)
                parsed = _parse_decision(text)
                if parsed:
                    tactical_result = parsed
                    tactical_score  = _DECISION_SCORE[parsed["decision"]] * parsed["confidence"]
                    logger.info("[%s] %s (tactisch): %s %.0f%% score=%+.2f",
                                market, tactical_prov, parsed["decision"],
                                parsed["confidence"] * 100, tactical_score)
                else:
                    logger.warning("[%s] %s: kon tactisch besluit niet parsen", market, tactical_prov)
            except Exception as exc:
                logger.warning("[%s] %s (tactisch) fout: %s", market, tactical_prov, exc)

        if tactical_result is None:
            return "HOLD", 0.0, "Geen tactisch analyse resultaat beschikbaar"

        # Snelle HOLD: score te laag en geen sentimentprovider
        sentiment_prov = next((p for p, r in roles.items() if r == "sentiment"), None)
        if not sentiment_prov and abs(tactical_score) < score_threshold:
            return "HOLD", abs(tactical_score), (
                f"Score {tactical_score:+.2f} onder drempel {score_threshold:.1f} — "
                f"{tactical_result['reasoning']}"
            )

        # ── Stap 2: Sentiment Analist (Gemini, altijd als geconfigureerd) ────────
        sentiment_result: dict | None = None
        sentiment_score = 0.0

        if sentiment_prov:
            try:
                text = complete_for(sentiment_prov, pdict[sentiment_prov],
                                    _SENTIMENT_PROMPT, prompt, max_tokens=512)
                logger.debug("[%s] %s raw: %.200s", market, sentiment_prov, text)
                parsed = _parse_sentiment(text)
                if parsed:
                    sentiment_result = parsed
                    sentiment_score  = _SENTIMENT_SCORE[parsed["sentiment"]] * parsed["confidence"]
                    logger.info("[%s] %s (sentiment): %s %.0f%% score=%+.2f",
                                market, sentiment_prov, parsed["sentiment"],
                                parsed["confidence"] * 100, sentiment_score)
                else:
                    logger.warning("[%s] %s: kon sentiment niet parsen", market, sentiment_prov)
            except Exception as exc:
                logger.warning("[%s] %s (sentiment) fout: %s", market, sentiment_prov, exc)

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
                parts.append(f"[{sentiment_prov}] {sentiment_result['reasoning']}")
            return "HOLD", abs(combined_score), (
                f"Score {combined_score:+.2f} onder drempel {score_threshold:.1f} — "
                + " | ".join(parts)
            )

        potential_action = "BUY" if combined_score > 0 else "SELL"

        # ── Stap 4: Risicomanager (Claude, alleen bij concrete trade) ─────────
        risk_prov = next((p for p, r in roles.items() if r == "risk"), None)
        final_confidence = min(abs(combined_score), 1.0)
        base_reasoning_parts = [f"[{tactical_prov}] {tactical_result['reasoning']}"]
        if sentiment_result:
            base_reasoning_parts.append(f"[{sentiment_prov}] {sentiment_result['reasoning']}")

        if risk_prov:
            risk_prompt = (
                f"Proposed action: {potential_action}\n"
                f"Combined score: {combined_score:+.2f} (threshold: {score_threshold:.1f})\n"
                f"Technical [{tactical_prov}]: {tactical_result['reasoning']}\n"
            )
            if sentiment_result:
                risk_prompt += f"Sentiment [{sentiment_prov}]: {sentiment_result['reasoning']}\n"
            risk_prompt += f"\nFull market context:\n{context}"

            try:
                text = complete_for(risk_prov, pdict[risk_prov],
                                    _RISK_PROMPT, risk_prompt, max_tokens=512)
                logger.debug("[%s] %s raw: %.200s", market, risk_prov, text)
                risk = _parse_risk(text)
                if risk:
                    logger.info("[%s] %s (risico): %s %.0f%% — %s",
                                market, risk_prov,
                                "GOEDGEKEURD" if risk["approved"] else "AFGEWEZEN",
                                risk["confidence"] * 100, risk["reasoning"])
                    if not risk["approved"]:
                        return "HOLD", risk["confidence"], (
                            f"[{risk_prov}] risicobeheer: {risk['reasoning']} | "
                            + " | ".join(base_reasoning_parts)
                        )
                    final_confidence = (abs(combined_score) + risk["confidence"]) / 2
                    base_reasoning_parts.append(f"[{risk_prov}] {risk['reasoning']}")
                else:
                    logger.warning("[%s] %s: kon risico-check niet parsen", market, risk_prov)
            except Exception as exc:
                logger.warning("[%s] %s (risico) fout: %s — trade gaat door op score", market, risk_prov, exc)

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
