"""Bitvavo API wrapper — read-only verbinding."""

import os
from python_bitvavo_api.bitvavo import Bitvavo
from dotenv import load_dotenv

load_dotenv()

# Bescherming tegen negatieve sleep-waarden als de Pi-klok fractie afwijkt van
# de Bitvavo-serverklok. De library berekent resetAt - now, wat negatief kan zijn.
_orig_wait = Bitvavo.waitForReset
def _safe_wait(self, wait_time):
    _orig_wait(self, max(0.0, float(wait_time)))
Bitvavo.waitForReset = _safe_wait


def get_client() -> Bitvavo:
    api_key = os.getenv("BITVAVO_API_KEY", "")
    api_secret = os.getenv("BITVAVO_API_SECRET", "")

    if not api_key or not api_secret:
        raise EnvironmentError(
            "Stel BITVAVO_API_KEY en BITVAVO_API_SECRET in via een .env bestand. "
            "Zie .env.example voor instructies."
        )

    return Bitvavo({
        "APIKEY": api_key,
        "APISECRET": api_secret,
        "RESTURL": "https://api.bitvavo.com/v2",
        "WSURL": "wss://ws.bitvavo.com/v2/",
        "ACCESSWINDOW": 10000,
        "DEBUGGING": False,
    })
