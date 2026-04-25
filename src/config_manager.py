"""Leest en schrijft .env configuratie-instellingen voor het web dashboard."""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"

DEFAULTS: dict[str, str] = {
    # Bitvavo
    "BITVAVO_API_KEY":          "",
    "BITVAVO_API_SECRET":       "",
    # Trading
    "TRADING_MARKETS":          "BTC-EUR,ETH-EUR",
    "CANDLE_INTERVAL":          "1h",
    "CHECK_INTERVAL_MINUTES":   "60",
    # Paper trading
    "PAPER_STARTING_CAPITAL":   "1000.0",
    "PAPER_TRADE_FRACTION":     "0.95",
    # Live trading
    "LIVE_TRADING_ENABLED":     "false",
    "MAX_TRADE_EUR":            "25",
    "MAX_EXPOSURE_EUR":         "100",
    "DAILY_LOSS_LIMIT_EUR":     "50",
    # Risk management
    "STOP_LOSS_PCT":            "",
    "TAKE_PROFIT_PCT":          "",
    "MTF_ENABLED":              "true",
    "VOL_SIZING_ENABLED":       "false",
    "CORR_CHECK_ENABLED":       "false",
    "CORR_THRESHOLD":           "0.8",
    # Home Assistant notificaties
    "HA_URL":                   "",
    "HA_TOKEN":                 "",
    "HA_NOTIFY_SERVICE":        "notify",
    # Claude AI / AI provider
    "AI_PROVIDER":              "anthropic",
    "AI_MODEL":                 "",
    "ANTHROPIC_API_KEY":        "",
    "GOOGLE_API_KEY":           "",
    "GROQ_API_KEY":             "",
    "AI_STRATEGY_ENABLED":      "false",
    "AI_MIN_CONFIDENCE":        "0.7",
    "AI_MAX_ORDERS_PER_DAY":    "3",
    "AI_COOLDOWN_MINUTES":      "60",
    # MQTT
    "MQTT_HOST":                "",
    "MQTT_PORT":                "1883",
    "MQTT_USER":                "",
    "MQTT_PASS":                "",
    "MQTT_PREFIX":              "bitvavo",
}

# Keys whose values should never be overwritten with an empty string via the UI
SENSITIVE_KEYS = {"BITVAVO_API_KEY", "BITVAVO_API_SECRET", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "MQTT_PASS", "HA_TOKEN"}

# Keys that map to HTML checkboxes (absent in POST = false)
BOOL_KEYS = {"LIVE_TRADING_ENABLED", "AI_STRATEGY_ENABLED", "MTF_ENABLED", "VOL_SIZING_ENABLED", "CORR_CHECK_ENABLED"}


def read_config() -> dict[str, str]:
    """Returns merged config: file values > os.environ > defaults."""
    config = dict(DEFAULTS)

    if ENV_PATH.exists():
        from dotenv import dotenv_values
        file_vals = dotenv_values(ENV_PATH)
        config.update({k: v for k, v in file_vals.items() if v is not None})

    # Running environment takes precedence (e.g. set via systemd)
    for key in DEFAULTS:
        val = os.environ.get(key)
        if val is not None:
            config[key] = val

    return config


def write_config(updates: dict[str, str]) -> None:
    """Write updated key-value pairs to the .env file."""
    from dotenv import set_key

    ENV_PATH.touch(exist_ok=True)
    for key, value in updates.items():
        set_key(str(ENV_PATH), key, value)


def config_from_form(form) -> dict[str, str]:
    """Build a validated updates dict from a Flask request.form."""
    updates: dict[str, str] = {}
    for key in DEFAULTS:
        if key in BOOL_KEYS:
            updates[key] = "true" if key in form else "false"
        else:
            value = form.get(key, "").strip()
            if value == "" and key in SENSITIVE_KEYS:
                continue  # leave existing secret untouched
            updates[key] = value
    return updates
