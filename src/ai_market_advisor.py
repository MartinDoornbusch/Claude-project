"""AI marktadviseur — vraagt Claude welke EUR-markten geschikt zijn voor automatisch traden."""

from __future__ import annotations

import json
import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None

_SYSTEM_PROMPT = """\
You are an expert crypto portfolio analyst for the Bitvavo exchange (Netherlands, EUR pairs).
Your task: analyze available EUR crypto markets and recommend which ones suit automated trading.

Selection criteria (in priority order):
1. EUR trading volume ≥ €50,000/day — essential for liquidity and tight spreads
2. Price momentum — moderate trend, not in extreme parabolic blow-off or total collapse
3. Volatility — enough for trading opportunities (~3–8% daily range is ideal)
4. Market quality — established projects with real utility, not pure speculation
5. Diversification — at most 1–2 large-caps (BTC/ETH), balance with mid-caps

Recommend 3–6 markets maximum. Concise reasoning per market (1 sentence max).

Respond ONLY with a JSON block in this exact format (no extra text):
```json
{
  "recommended": ["BTC-EUR", "ETH-EUR", "SOL-EUR"],
  "summary": "One sentence describing the overall portfolio rationale.",
  "markets": {
    "BTC-EUR":  {"include": true,  "confidence": 0.95, "reasoning": "Highest liquidity, stable trend."},
    "ETH-EUR":  {"include": true,  "confidence": 0.88, "reasoning": "Strong ecosystem, good volume."},
    "DOGE-EUR": {"include": false, "reasoning": "Purely speculative, high pump-dump risk."}
  }
}
```
Rules: include only analyzed markets in "markets". "confidence" (0.0–1.0) only for included=true markets.\
"""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY niet ingesteld in .env")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _build_market_table(market_stats: list[dict], limit: int = 40) -> str:
    lines = [
        f"Top {min(limit, len(market_stats))} EUR markets on Bitvavo by 24h volume:",
        "",
        f"{'Market':<12} {'Price':>12} {'24h %':>8} {'Volume EUR':>16}",
        "-" * 52,
    ]
    for m in market_stats[:limit]:
        change = m.get("change_24h", 0)
        lines.append(
            f"{m['market']:<12} "
            f"€{m['price']:>11.4f} "
            f"{change:>+7.2f}% "
            f"€{m['volume_eur']:>15,.0f}"
        )
    return "\n".join(lines)


def _parse_advice(text: str) -> dict | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        match = re.search(r'\{[^{}]*"recommended".*?\}', text, re.DOTALL)
        if not match:
            return None
        json_str = match.group(0)
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None


def advise_markets(market_stats: list[dict]) -> dict:
    """
    Vraagt Claude welke markten het meest geschikt zijn voor automatisch traden.

    Returns dict with:
      recommended: list[str]       — aanbevolen marktparen
      summary:     str             — korte samenvatting
      markets:     dict[str, dict] — per-markt advies {include, confidence, reasoning}
    """
    if not market_stats:
        return {"recommended": [], "summary": "Geen marktdata beschikbaar.", "markets": {}}

    client = _get_client()
    table = _build_market_table(market_stats)

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": (
                "Analyze these markets and recommend which ones to include "
                "in an automated EUR trading portfolio:\n\n" + table
            ),
        }],
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    logger.debug("AI marktadvies raw: %.500s", text)

    parsed = _parse_advice(text)
    if not parsed:
        logger.warning("Kon AI marktadvies niet parsen: %.300s", text)
        return {
            "recommended": [],
            "summary": "Advies kon niet worden geparsed — probeer opnieuw.",
            "markets": {},
        }

    logger.info(
        "AI marktadvies: %d aanbevolen — %s",
        len(parsed.get("recommended", [])),
        parsed.get("summary", ""),
    )
    return parsed
