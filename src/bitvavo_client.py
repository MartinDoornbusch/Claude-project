"""Bitvavo API wrapper — read-only verbinding."""

import os
from python_bitvavo_api.bitvavo import Bitvavo
from dotenv import load_dotenv

load_dotenv()


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
