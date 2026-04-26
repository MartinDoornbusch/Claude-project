"""Robuuste hulpfuncties voor het lezen van omgevingsvariabelen.

Vangt drie veelvoorkomende problemen op:
- Lege string (variabele bestaat maar heeft geen waarde)
- Komma als decimaalscheidingsteken (Nederlandse input)
- Niet-numerieke restwaarden
"""

from __future__ import annotations

import os


def env_float(key: str, default: float) -> float:
    """Lees omgevingsvariabele als float; veilig voor lege strings en komma's."""
    raw = os.getenv(key, "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def env_int(key: str, default: int) -> int:
    """Lees omgevingsvariabele als int; veilig voor lege strings."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        # Also handle e.g. "60.0" stored by the UI
        return int(float(raw.replace(",", ".")))
    except (ValueError, TypeError):
        return default


def env_float_opt(key: str) -> float | None:
    """Lees omgevingsvariabele als float; retourneert None als leeg of afwezig."""
    raw = os.getenv(key, "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None
