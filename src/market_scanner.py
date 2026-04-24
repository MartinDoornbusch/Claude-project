"""Marktverkenner — haalt alle beschikbare EUR-markten op van Bitvavo."""

from __future__ import annotations

import logging
from python_bitvavo_api.bitvavo import Bitvavo

logger = logging.getLogger(__name__)

# Minimum 24u EUR volume om in aanmerking te komen
MIN_VOLUME_EUR = 10_000


def get_all_eur_markets(client: Bitvavo) -> list[str]:
    """Retourneert alle actieve EUR handelsparen van Bitvavo."""
    markets = client.markets({})
    if isinstance(markets, dict) and "error" in markets:
        raise RuntimeError(f"Bitvavo fout: {markets['error']}")
    return sorted(
        m["market"] for m in markets
        if isinstance(m, dict)
        and m.get("market", "").endswith("-EUR")
        and m.get("status") == "trading"
    )


def get_market_stats(client: Bitvavo, markets: list[str] | None = None) -> list[dict]:
    """
    Haalt 24u ticker statistieken op voor alle (of opgegeven) EUR-markten.
    Retourneert lijst gesorteerd op volume (hoogste eerst).
    Één API-call voor alle tickers tegelijk.
    """
    params = {"market": markets[0]} if markets and len(markets) == 1 else {}
    tickers = client.ticker24h(params)

    if isinstance(tickers, dict):
        tickers = [tickers]

    if not isinstance(tickers, list):
        logger.warning("Onverwacht ticker-formaat: %s", type(tickers))
        return []

    results = []
    for t in tickers:
        if not isinstance(t, dict) or "error" in t:
            continue

        market = t.get("market", "")
        if not market.endswith("-EUR"):
            continue
        if markets and market not in markets:
            continue

        try:
            price      = float(t.get("last")       or 0)
            open_price = float(t.get("open")        or price)
            volume_eur = float(t.get("volumeQuote") or 0)
            high       = float(t.get("high")        or 0)
            low        = float(t.get("low")         or 0)
            change_pct = ((price - open_price) / open_price * 100) if open_price else 0

            results.append({
                "market":     market,
                "price":      price,
                "change_24h": round(change_pct, 2),
                "volume_eur": round(volume_eur, 0),
                "high_24h":   high,
                "low_24h":    low,
            })
        except (TypeError, ValueError) as exc:
            logger.debug("Stats-verwerking mislukt voor %s: %s", market, exc)

    return sorted(results, key=lambda x: x["volume_eur"], reverse=True)


def get_tradeable_markets(client: Bitvavo, min_volume: float = MIN_VOLUME_EUR) -> list[dict]:
    """
    Retourneert EUR-markten die voldoen aan het minimale dagvolume.
    Handig als startpunt voor AI-advies.
    """
    stats = get_market_stats(client)
    return [m for m in stats if m["volume_eur"] >= min_volume]
