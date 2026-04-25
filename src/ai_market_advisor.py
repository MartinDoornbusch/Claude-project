"""AI marktadviseur — vraagt de geconfigureerde AI welke EUR-markten geschikt zijn voor automatisch traden."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert crypto portfolio analyst for the Bitvavo exchange (Netherlands, EUR pairs).
Your task: evaluate ALL submitted EUR markets and mark each one as suitable (include: true) or unsuitable (include: false) for automated short-term trading.

Inclusion criteria — include: true when ALL of these hold:
1. EUR trading volume ≥ €50,000/day (liquidity, tight spreads)
2. Real project utility — NOT a stablecoin (USDT, USDC, DAI, BUSD, TUSD, FRAX, USDD, EURC, etc.)
3. Sufficient volatility for trading opportunities (typically > 1% daily movement)
4. Not in extreme parabolic blow-off or total collapse
5. Established market with reasonable track record

Exclusion — include: false when ANY of these hold:
- It is a stablecoin or fiat-pegged token → reasoning: "Stablecoin — no trading opportunity"
- Volume < €20,000/day → reasoning: "Insufficient volume"
- Pure meme / no utility / rug-pull risk → reasoning: "Speculative/meme — no utility"
- Extreme movement (>40% in 24h) suggesting pump-dump → reasoning: "Extreme pump/dump risk"

You MUST evaluate every market in the list. Aim to include all markets that genuinely meet the criteria — typically 15–35% of the submitted list.

Respond ONLY with a JSON block in this exact format (no extra text):
```json
{
  "recommended": ["BTC-EUR", "ETH-EUR", "SOL-EUR"],
  "summary": "One sentence describing the overall selection rationale.",
  "markets": {
    "BTC-EUR":  {"include": true,  "confidence": 0.95, "reasoning": "Highest liquidity, solid trend."},
    "USDT-EUR": {"include": false, "reasoning": "Stablecoin — no trading opportunity."},
    "DOGE-EUR": {"include": false, "reasoning": "Meme coin — high speculation risk."}
  }
}
```
Rules: include ALL analyzed markets in "markets". "confidence" (0.0–1.0) only for include=true entries.\
"""


_STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD", "FRAX", "USDD", "USTC",
    "EURC", "EURT", "EURS", "AGEUR", "XAUT", "PAXG", "WBTC", "WETH", "STETH",
}


def _is_stablecoin(market: str) -> bool:
    base = market.split("-")[0].upper()
    return base in _STABLECOINS


def _build_market_table(market_stats: list[dict], limit: int = 80) -> str:
    # Toon stablecoins wel in de tabel zodat de AI ze expliciet kan uitsluiten
    top = market_stats[:limit]
    lines = [
        f"Top {len(top)} EUR markets on Bitvavo by 24h volume:",
        "",
        f"{'Market':<14} {'Price':>12} {'24h %':>8} {'Volume EUR':>16} {'Note':<12}",
        "-" * 64,
    ]
    for m in top:
        change = m.get("change_24h", 0) or 0
        note = "STABLECOIN" if _is_stablecoin(m["market"]) else ""
        lines.append(
            f"{m['market']:<14} "
            f"€{m['price']:>11.4f} "
            f"{change:>+7.2f}% "
            f"€{m['volume_eur']:>15,.0f} "
            f"{note:<12}"
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


def advise_markets(market_stats: list[dict], *, provider: str | None = None, model: str | None = None) -> dict:
    """
    Vraagt de opgegeven (of actieve) AI welke markten het meest geschikt zijn voor automatisch traden.

    Returns dict with:
      recommended: list[str]       — aanbevolen marktparen
      summary:     str             — korte samenvatting
      markets:     dict[str, dict] — per-markt advies {include, confidence, reasoning}
    """
    if not market_stats:
        return {"recommended": [], "summary": "Geen marktdata beschikbaar.", "markets": {}}

    from src.ai_provider import complete_for, get_active
    if provider is None:
        provider, model = get_active()
    logger.info("AI marktadvies via provider=%s model=%s", provider, model)

    table = _build_market_table(market_stats)
    text = complete_for(
        provider, model,
        _SYSTEM_PROMPT,
        "Analyze these markets and recommend which ones to include "
        "in an automated EUR trading portfolio:\n\n" + table,
        max_tokens=2048,
    )
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
