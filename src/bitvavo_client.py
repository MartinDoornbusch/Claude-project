"""Bitvavo API wrapper — read-only verbinding."""

import os
import time as _time
from python_bitvavo_api.bitvavo import Bitvavo
from dotenv import load_dotenv

load_dotenv()

# De Bitvavo-library berekent rate-limit wachttijden als
# (server_reset_timestamp_UTC - lokale_tijd). Als de systeemklok op CEST staat
# (UTC+2) en de server een UTC-timestamp stuurt, kan de uitkomst negatief zijn,
# wat leidt tot ValueError: sleep length must be non-negative.
# Clamp alle sleep-aanroepen naar ≥ 0 als preventie.
_orig_sleep = _time.sleep


def _safe_sleep(secs):
    _orig_sleep(max(0.0, secs))


_time.sleep = _safe_sleep


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
