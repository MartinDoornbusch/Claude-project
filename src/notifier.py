"""HA notifier — stuurt push-notificaties via de Home Assistant REST API."""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

HA_URL     = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN   = os.getenv("HA_TOKEN", "")
HA_SERVICE = os.getenv("HA_NOTIFY_SERVICE", "notify")


def _enabled() -> bool:
    return bool(HA_URL and HA_TOKEN)


def send(title: str, message: str) -> bool:
    """Stuur een push-notificatie via HA notify service. Geeft True bij succes."""
    if not _enabled():
        return False
    try:
        url = f"{HA_URL}/api/services/notify/{HA_SERVICE}"
        resp = requests.post(
            url,
            json={"title": title, "message": message},
            headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
            timeout=5,
        )
        resp.raise_for_status()
        logger.debug("HA notificatie verstuurd: %s", title)
        return True
    except Exception as exc:
        logger.warning("HA notificatie mislukt: %s", exc)
        return False


def notify_trade(market: str, side: str, price: float, reason: str) -> None:
    emoji = "🟢" if side == "BUY" else "🔴"
    send(
        title=f"{emoji} Bot: {side} {market}",
        message=f"Prijs: €{price:.4f}\nReden: {reason}",
    )


def notify_sl_tp(market: str, trigger: str, chg_pct: float, price: float) -> None:
    emoji = "🛑" if "Stop" in trigger else "✅"
    send(
        title=f"{emoji} {trigger} — {market}",
        message=f"Prijs: €{price:.4f}  ({chg_pct:+.1f}%)",
    )


def notify_error(market: str, error: str) -> None:
    send(title=f"⚠️ Bot fout — {market}", message=str(error)[:200])
