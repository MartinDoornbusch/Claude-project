"""Portfolio ophalen en tonen via Bitvavo REST API."""

from __future__ import annotations

from python_bitvavo_api.bitvavo import Bitvavo


def get_balances(client: Bitvavo) -> list[dict]:
    """Geeft alle balances terug met saldo > 0."""
    balances = client.balance({})
    if isinstance(balances, dict) and "error" in balances:
        raise RuntimeError(f"Bitvavo fout: {balances['error']} — {balances.get('errorCode', '')}")
    return [b for b in balances if float(b.get("available", 0)) > 0 or float(b.get("inOrder", 0)) > 0]


def get_ticker_price(client: Bitvavo, market: str) -> float | None:
    """Haal de actuele prijs op voor een handelspaar (bijv. 'BTC-EUR')."""
    result = client.tickerPrice({"market": market})
    if isinstance(result, dict) and "price" in result:
        return float(result["price"])
    return None


def get_portfolio_value_eur(client: Bitvavo) -> tuple[list[dict], float]:
    """
    Haal balances op en bereken totale EUR-waarde.
    Retourneert (verrijkte balances, totaal_eur).
    """
    balances = get_balances(client)
    enriched = []
    total_eur = 0.0

    for b in balances:
        symbol = b["symbol"]
        available = float(b["available"])
        in_order = float(b["inOrder"])
        total = available + in_order

        if symbol == "EUR":
            eur_value = total
        else:
            price = get_ticker_price(client, f"{symbol}-EUR")
            eur_value = total * price if price else 0.0

        total_eur += eur_value
        enriched.append({
            "symbol": symbol,
            "available": available,
            "inOrder": in_order,
            "total": total,
            "eurValue": eur_value,
        })

    enriched.sort(key=lambda x: x["eurValue"], reverse=True)
    return enriched, total_eur
