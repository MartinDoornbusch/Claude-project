"""AI strategie — gebruikt Claude claude-opus-4-7 als trading brein met guardrails."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

import anthropic

from src.database import (
    get_latest_signals, get_cash, get_position, get_paper_trades,
    get_last_buy_ts, get_recent_trade_pairs, get_market_change_24h,
)

logger = logging.getLogger(__name__)

AI_ENABLED         = os.getenv("AI_STRATEGY_ENABLED", "false").lower() == "true"
MIN_CONFIDENCE     = float(os.getenv("AI_MIN_CONFIDENCE", "0.7"))
MAX_ORDERS_PER_DAY = int(os.getenv("AI_MAX_ORDERS_PER_DAY", "3"))
COOLDOWN_MINUTES   = int(os.getenv("AI_COOLDOWN_MINUTES", "60"))

_client: anthropic.Anthropic | None = None

_SYSTEM_PROMPT = """\
You are an expert crypto trading analyst for the Bitvavo exchange.
Your task is to evaluate technical indicator data and return a disciplined trading decision.

Trading rules:
- Recommend BUY only when there is strong bullish confluence (e.g. golden cross + RSI not overbought, \
price near lower Bollinger Band with rising MACD)
- Recommend SELL only when there is strong bearish confluence (e.g. death cross, RSI overbought > 70, \
or significant unrealized loss threatening the daily loss limit)
- Default to HOLD when signals are mixed, unclear, or there is insufficient data
- Capital preservation is the priority — missing a move is better than a bad trade
- Do NOT buy if there is already an open position unless the signal is exceptionally strong
- Do NOT sell if there is no open position

You MUST respond with ONLY a JSON block in this exact format (no extra text):
```json
{
  "decision": "BUY",
  "confidence": 0.82,
  "reasoning": "Golden cross confirmed with RSI at 45 — not overbought, clear upward momentum."
}
```
decision must be one of: BUY, SELL, HOLD
confidence must be a float between 0.0 and 1.0
reasoning must be a single concise sentence in English\
"""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY niet ingesteld in .env")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _orders_executed_today(market: str) -> int:
    from src.database import get_ai_decisions_today
    return get_ai_decisions_today(market)


def _last_trade_minutes_ago(market: str) -> float | None:
    trades = get_paper_trades(market, limit=1)
    if not trades:
        return None
    ts = datetime.fromisoformat(trades[0]["ts"])
    return (datetime.utcnow() - ts).total_seconds() / 60


def _build_context(market: str, signals: dict, recent_signals: list[dict], fg_str: str = "") -> str:
    pos   = get_position(market)
    cash  = get_cash()
    price = float(signals.get("close", 0))

    lines = [
        f"Market: {market}",
        f"Current price: €{price:.4f}",
    ]

    # 24u koersverandering
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
        rsi = signals["rsi_14"]
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
            bb_mid   = (signals["bb_lower"] + signals["bb_upper"]) / 2
            bb_pos   = f"inside bands ({'upper half' if price > bb_mid else 'lower half'})"
        lines.append(
            f"Bollinger Bands: €{signals['bb_lower']:.4f} — €{signals['bb_upper']:.4f}  ({bb_pos})"
        )

    ma_cross = signals.get("ma_cross")
    if ma_cross:
        cross_label = "GOLDEN CROSS (bullish)" if ma_cross == "golden_cross" else "DEATH CROSS (bearish)"
        lines.append(f"MA Cross signal: {cross_label}")

    # Volume vs gemiddelde
    vol     = signals.get("volume")
    vol_avg = signals.get("volume_avg_20")
    if vol is not None and vol_avg and vol_avg > 0:
        vol_ratio = vol / vol_avg
        vol_label = "HIGH ↑↑" if vol_ratio > 1.5 else ("above average ↑" if vol_ratio > 1.1 else
                    ("below average ↓" if vol_ratio < 0.9 else "average"))
        lines.append(f"Volume: {vol:,.0f}  ({vol_ratio:.1f}× 20-period avg — {vol_label})")

    # ATR / volatiliteit
    atr = signals.get("atr_14")
    if atr is not None and price > 0:
        atr_pct = atr / price * 100
        vol_level = "HIGH volatility ⚠" if atr_pct > 4 else ("elevated" if atr_pct > 2 else "low/normal")
        lines.append(f"ATR-14: €{atr:.4f} ({atr_pct:.2f}% of price — {vol_level})")

    lines += ["", "=== Portfolio State ===",
              f"Available cash: €{cash:.2f}",
              f"Open position: {pos['amount']:.6f} units @ avg €{pos['avg_price']:.4f}"]

    if pos["amount"] > 0 and pos["avg_price"] > 0:
        pnl_eur = (price - pos["avg_price"]) * pos["amount"]
        pnl_pct = (price - pos["avg_price"]) / pos["avg_price"] * 100
        lines.append(f"Unrealized PnL: €{pnl_eur:+.2f} ({pnl_pct:+.2f}%)")

        # Tijd in huidige positie
        buy_ts = get_last_buy_ts(market)
        if buy_ts:
            try:
                elapsed = datetime.utcnow() - datetime.fromisoformat(buy_ts[:19])
                hours   = int(elapsed.total_seconds() / 3600)
                days    = hours // 24
                duration_str = f"{days}d {hours % 24}h" if days > 0 else f"{hours}h"
                lines.append(f"Position open for: {duration_str}")
            except Exception:
                pass

    # Terugkoppeling: recente afgesloten trades
    past_pairs = get_recent_trade_pairs(market, limit=5)
    if past_pairs:
        wins   = sum(1 for p in past_pairs if p["pnl_eur"] > 0)
        total  = len(past_pairs)
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

    if recent_signals:
        lines += ["", "=== Recent Signal History (newest first) ==="]
        for s in recent_signals[:5]:
            lines.append(
                f"  {s['ts'][:16]}  signal={s.get('signal', 'n/a'):<4}  "
                f"price=€{s.get('close', 0):.4f}  RSI={s.get('rsi_14') or 'n/a'}"
            )

    return "\n".join(lines)


def _parse_decision(text: str) -> dict | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        match = re.search(r'\{[^{}]*"decision"[^{}]*\}', text, re.DOTALL)
        if not match:
            return None
        json_str = match.group(0)

    try:
        data = json.loads(json_str)
        decision = str(data.get("decision", "HOLD")).upper()
        if decision not in ("BUY", "SELL", "HOLD"):
            decision = "HOLD"
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        reasoning = str(data.get("reasoning", ""))
        return {"decision": decision, "confidence": confidence, "reasoning": reasoning}
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def ai_evaluate(market: str, signals: dict) -> tuple[str, float, str]:
    """
    Vraagt Claude om een trading beslissing voor de opgegeven markt.

    Retourneert: (decision, confidence, reasoning)
    - decision:   "BUY" | "SELL" | "HOLD"
    - confidence: 0.0–1.0
    - reasoning:  uitleg van de beslissing
    """
    if not AI_ENABLED:
        return "HOLD", 0.0, "AI strategie uitgeschakeld"

    if _orders_executed_today(market) >= MAX_ORDERS_PER_DAY:
        logger.info("[%s] AI: dagelijks maximum van %d orders bereikt", market, MAX_ORDERS_PER_DAY)
        return "HOLD", 0.0, f"Max {MAX_ORDERS_PER_DAY} orders per dag bereikt"

    minutes_ago = _last_trade_minutes_ago(market)
    if minutes_ago is not None and minutes_ago < COOLDOWN_MINUTES:
        remaining = int(COOLDOWN_MINUTES - minutes_ago)
        logger.info("[%s] AI: cooldown actief, nog %d minuten", market, remaining)
        return "HOLD", 0.0, f"Cooldown: wacht nog {remaining} minuten"

    recent_signals = get_latest_signals(market, limit=5)

    from src.sentiment import get_fear_greed, fmt_fear_greed
    fg_str = fmt_fear_greed(get_fear_greed())

    context = _build_context(market, signals, recent_signals, fg_str)

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": (
                    "Analyze the following market data and return your trading decision:\n\n"
                    + context
                ),
            }],
        )

        text = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        )
        logger.debug("[%s] AI raw response: %.300s", market, text)

        parsed = _parse_decision(text)
        if not parsed:
            logger.warning("[%s] AI: kon besluit niet parsen uit respons: %.200s", market, text)
            return "HOLD", 0.0, "Kon AI respons niet parsen"

        decision   = parsed["decision"]
        confidence = parsed["confidence"]
        reasoning  = parsed["reasoning"]

        cache_hit = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        logger.debug("[%s] AI cache_read_tokens=%d", market, cache_hit)

        if confidence < MIN_CONFIDENCE:
            logger.info(
                "[%s] AI advies %s afgewezen: confidence %.0f%% < minimum %.0f%%",
                market, decision, confidence * 100, MIN_CONFIDENCE * 100,
            )
            return (
                "HOLD", confidence,
                f"Confidence {confidence:.0%} onder minimum {MIN_CONFIDENCE:.0%}: {reasoning}",
            )

        logger.info(
            "[%s] AI besluit: %s (%.0f%%) — %s",
            market, decision, confidence * 100, reasoning,
        )
        return decision, confidence, reasoning

    except anthropic.AuthenticationError:
        logger.error("AI: Anthropic API key ongeldig")
        return "HOLD", 0.0, "Anthropic API key ongeldig"
    except anthropic.RateLimitError:
        logger.warning("AI: Anthropic rate limit bereikt")
        return "HOLD", 0.0, "Anthropic rate limit bereikt"
    except anthropic.APIConnectionError:
        logger.warning("AI: geen verbinding met Anthropic API")
        return "HOLD", 0.0, "Geen verbinding met Anthropic API"
    except EnvironmentError as exc:
        logger.error("AI configuratiefout: %s", exc)
        return "HOLD", 0.0, str(exc)
    except Exception as exc:
        logger.error("[%s] AI onverwachte fout: %s", market, exc, exc_info=True)
        return "HOLD", 0.0, f"AI fout: {exc}"
