"""Marktsentiment — Fear & Greed Index van alternative.me."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/?limit=1"


def get_fear_greed() -> dict | None:
    """
    Haalt de huidige Fear & Greed Index op.
    Retourneert {'value': 45, 'classification': 'Fear'} of None bij fout.
    """
    try:
        r = requests.get(_FNG_URL, timeout=5)
        data = r.json()["data"][0]
        return {
            "value": int(data["value"]),
            "classification": data["value_classification"],
        }
    except Exception as exc:
        logger.warning("Fear & Greed ophalen mislukt: %s", exc)
        return None


def fmt_fear_greed(fg: dict | None) -> str:
    """Geeft een leesbare string terug voor gebruik in AI-context."""
    if not fg:
        return ""
    v, c = fg["value"], fg["classification"]
    if v < 25:
        emoji = "😱 Extreme Fear"
    elif v < 45:
        emoji = "😟 Fear"
    elif v < 55:
        emoji = "😐 Neutral"
    elif v < 75:
        emoji = "😊 Greed"
    else:
        emoji = "🤑 Extreme Greed"
    return f"Fear & Greed Index: {v}/100 — {emoji} ({c})"
