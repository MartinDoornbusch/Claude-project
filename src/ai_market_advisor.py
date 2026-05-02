"""AI marktadviseur — vraagt de geconfigureerde AI welke EUR-markten geschikt zijn voor automatisch traden."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert crypto portfolio analyst for the Bitvavo exchange (Netherlands, EUR pairs).
Your task: evaluate ALL submitted EUR markets and select those suitable for automated short-term trading.

Inclusion criteria (include when ALL hold):
1. EUR trading volume ≥ €50,000/day — tight spreads, good liquidity
2. Real project utility — NOT a stablecoin (USDT, USDC, DAI, BUSD, TUSD, FRAX, USDD, EURC, etc.)
3. Sufficient volatility for trading opportunities (> 1% daily movement typical)
4. Not in extreme parabolic blow-off or total collapse (> 40% move in 24h = skip)
5. Established project with reasonable track record

Exclusion (never include):
- Stablecoins or fiat-pegged tokens → they don't move
- Volume < €20,000/day → too illiquid for reliable execution
- Pure meme / no utility / high rug-pull risk
- Extreme pump-dump (> 40% in 24h)

Target 15–35% of the submitted markets.

Respond ONLY with raw JSON — no markdown, no code blocks, no text before or after.
Start your response with { and end with }.
Output format (fill in values):
{"recommended": ["BTC-EUR", "ETH-EUR", "SOL-EUR"], "summary": "One sentence rationale.", "markets": {"BTC-EUR": {"confidence": 0.95, "reasoning": "brief reason"}, "ETH-EUR": {"confidence": 0.90, "reasoning": "brief reason"}}}
Keep each "reasoning" under 10 words. List only recommended markets in "markets".\
"""


_STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD", "FRAX", "USDD", "USTC",
    "EURC", "EURT", "EURS", "AGEUR", "XAUT", "PAXG", "WBTC", "WETH", "STETH",
}


def _is_stablecoin(market: str) -> bool:
    base = market.split("-")[0].upper()
    return base in _STABLECOINS


def _build_market_table(market_stats: list[dict], limit: int = 20, min_volume_eur: float = 20_000) -> str:
    # Filter stablecoins en extreem lage volumes vóór verzending naar AI
    filtered = [
        m for m in market_stats
        if not _is_stablecoin(m["market"]) and (m.get("volume_eur") or 0) >= min_volume_eur
    ]
    top = filtered[:limit]
    lines = [
        f"Top {len(top)} EUR markets on Bitvavo by 24h volume (stablecoins and <€{min_volume_eur:,.0f}/day excluded):",
        "",
        f"{'Market':<14} {'Price':>12} {'24h %':>8} {'Volume EUR':>16}",
        "-" * 54,
    ]
    for m in top:
        change = m.get("change_24h", 0) or 0
        lines.append(
            f"{m['market']:<14} "
            f"€{m['price']:>11.4f} "
            f"{change:>+7.2f}% "
            f"€{m['volume_eur']:>15,.0f}"
        )
    return "\n".join(lines)


def _parse_advice(text: str) -> dict | None:
    """Parseer AI-antwoord; valt stapsgewijs terug bij truncatie of code-fences."""
    if not text:
        return None

    # Strip code-fences (Gemini kopieert ze als het prompt ze toont)
    cleaned = re.sub(r"```(?:json)?\s*|```", "", text).strip()

    # Poging 1: volledige JSON via greedy-match op het buitenste object
    m = re.search(r'\{\s*"recommended"\s*:.*\}', cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    # Poging 2: brace-depth scan — robuuster bij geneste objecten
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
                    if '"recommended"' in candidate:
                        try:
                            return json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    break

    # Poging 3: afgekapte array — geen sluitende ] nodig; extraheer marktnamen via regex
    rec_m = re.search(r'"recommended"\s*:\s*\[([^\]]*)', cleaned, re.DOTALL)
    sum_m = re.search(r'"summary"\s*:\s*"([^"]*)"', cleaned)
    if rec_m:
        markets_found = re.findall(r'"([A-Z0-9]+-EUR)"', rec_m.group(1))
        if markets_found:
            summary = sum_m.group(1) if sum_m else "Advies gedeeltelijk ontvangen — response afgekapt."
            logger.warning("AI marktadvies afgekapt — %d markten via regex geëxtraheerd", len(markets_found))
            return {"recommended": markets_found, "summary": summary, "markets": {}}

    return None


def advise_markets(market_stats: list[dict], *, provider: str | None = None, model: str | None = None) -> dict:
    """
    Vraagt de opgegeven (of actieve) AI welke markten het meest geschikt zijn voor automatisch traden.

    Returns dict with:
      recommended: list[str]       — aanbevolen marktparen
      summary:     str             — korte samenvatting
      markets:     dict[str, dict] — per-markt advies voor aanbevolen markten {confidence, reasoning}
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
        max_tokens=400,
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
