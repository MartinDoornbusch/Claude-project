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
    "TRADING_BLACKLIST":        "",
    "CANDLE_INTERVAL":          "1h",
    "CHECK_INTERVAL_MINUTES":   "60",
    # Paper trading
    "PAPER_STARTING_CAPITAL":   "1000.0",
    "PAPER_TRADE_FRACTION":     "0.15",
    # Position sizing
    "POSITION_SIZING_MODE":     "fraction",   # "fraction" | "risk_pct"
    "RISK_PER_TRADE_PCT":       "1.0",
    # Live trading
    "LIVE_TRADING_ENABLED":     "false",
    "MAX_TRADE_EUR":            "25",
    "MAX_EXPOSURE_EUR":         "100",
    # Risk management
    "DAILY_LOSS_LIMIT_PCT":     "2.0",
    "CIRCUIT_BREAKER_PCT":      "0",
    "STOP_LOSS_PCT":            "",
    "TAKE_PROFIT_PCT":          "",
    "OCO_ENABLED":              "false",
    "TRAILING_STOP_ENABLED":    "false",
    "TRAILING_STOP_PCT":        "2.0",
    "BREAKEVEN_TRIGGER_PCT":    "",
    # House money / portfolio
    "HOUSE_MONEY_ENABLED":      "false",
    "HOUSE_MONEY_TRIGGER_PCT":  "10",
    "HOUSE_MONEY_ONLY_PROFIT":  "false",
    "WIN_EXCL_COOLDOWN_HOURS":  "6",
    "MIN_ORDER_EUR":            "5",
    "CLEANUP_PCT":              "50",
    "ICEBERG_ENABLED":              "false",
    "ICEBERG_THRESHOLD":            "500",
    "MIN_ICEBERG_CHUNK":            "100",
    "ICEBERG_VARIANCE":             "0.15",
    "ICEBERG_INTERVAL_SECONDS":     "2",
    "ICEBERG_SLIPPAGE_GUARD_PCT":   "0.5",
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
    "MISTRAL_API_KEY":          "",
    "CEREBRAS_API_KEY":         "",
    "AI_ANTHROPIC_ENABLED":     "true",
    "AI_GOOGLE_ENABLED":        "true",
    "AI_GROQ_ENABLED":          "true",
    "AI_MISTRAL_ENABLED":       "true",
    "AI_CEREBRAS_ENABLED":      "true",
    "AI_MODEL_ANTHROPIC":       "",
    "AI_MODEL_GOOGLE":          "",
    "AI_MODEL_GROQ":            "",
    "AI_MODEL_MISTRAL":         "",
    "AI_MODEL_CEREBRAS":        "",
    "GOOGLE_DAILY_LIMIT":       "1500",
    "MISTRAL_DAILY_LIMIT":      "500000",
    "CEREBRAS_DAILY_LIMIT":     "1000000",
    "AI_STRATEGY_ENABLED":      "false",
    "AI_MIN_CONFIDENCE":        "0.85",
    "AI_MAX_ORDERS_PER_DAY":    "3",
    "AI_COOLDOWN_MINUTES":      "60",
    "AI_SCORE_THRESHOLD":       "0.5",
    "GEMINI_GATE_SCORE":        "0.5",
    "ALT_MARKETS":              "",
    "ALT_THRESHOLD_MULTIPLIER": "1.5",
    "AI_CALL_DELAY_SECONDS":        "1.0",
    "AI_ACCURACY_HORIZON_HOURS":    "8",
    "TREND_FILTER_ENABLED":         "1",
    "SENTIMENT_VETO_CONF":          "0.6",
    "TRADE_HOURS_START":        "6",
    "TRADE_HOURS_END":          "23",
    "ATR_FLAT_THRESHOLD":       "0.5",
    "ATR_SENSITIVITY":          "0.8",
    "MIN_VOLUME_EUR":           "0",
    # MQTT
    "MQTT_ENABLED":             "true",
    "MQTT_HOST":                "",
    "MQTT_PORT":                "1883",
    "MQTT_USER":                "",
    "MQTT_PASS":                "",
    "MQTT_PREFIX":              "bitvavo",
    "MQTT_CONNECT_TIMEOUT":     "3",
}

# Keys whose values should never be overwritten with an empty string via the UI
SENSITIVE_KEYS = {
    "BITVAVO_API_KEY", "BITVAVO_API_SECRET",
    "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
    "MISTRAL_API_KEY", "CEREBRAS_API_KEY",
    "MQTT_PASS", "HA_TOKEN",
}

# Keys that map to HTML checkboxes (absent in POST = false)
BOOL_KEYS = {"LIVE_TRADING_ENABLED", "AI_STRATEGY_ENABLED", "MTF_ENABLED", "VOL_SIZING_ENABLED",
             "CORR_CHECK_ENABLED", "OCO_ENABLED", "ICEBERG_ENABLED", "TRAILING_STOP_ENABLED",
             "HOUSE_MONEY_ENABLED", "HOUSE_MONEY_ONLY_PROFIT",
             "AI_ANTHROPIC_ENABLED", "AI_GOOGLE_ENABLED", "AI_GROQ_ENABLED",
             "AI_MISTRAL_ENABLED", "AI_CEREBRAS_ENABLED",
             "MQTT_ENABLED"}


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


_NUMERIC_KEYS = {
    "CHECK_INTERVAL_MINUTES", "PAPER_STARTING_CAPITAL", "PAPER_TRADE_FRACTION",
    "RISK_PER_TRADE_PCT", "MAX_TRADE_EUR", "MAX_EXPOSURE_EUR",
    "DAILY_LOSS_LIMIT_PCT", "CIRCUIT_BREAKER_PCT", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
    "TRAILING_STOP_PCT", "BREAKEVEN_TRIGGER_PCT",
    "HOUSE_MONEY_TRIGGER_PCT", "CLEANUP_PCT",
    "ICEBERG_THRESHOLD", "MIN_ICEBERG_CHUNK", "ICEBERG_VARIANCE", "ICEBERG_INTERVAL_SECONDS", "ICEBERG_SLIPPAGE_GUARD_PCT",
    "AI_MIN_CONFIDENCE", "AI_MAX_ORDERS_PER_DAY", "AI_COOLDOWN_MINUTES", "AI_SCORE_THRESHOLD", "GEMINI_GATE_SCORE", "ALT_THRESHOLD_MULTIPLIER",
    "TRADE_HOURS_START", "TRADE_HOURS_END",
    "ATR_FLAT_THRESHOLD", "ATR_SENSITIVITY", "CORR_THRESHOLD", "MQTT_PORT", "MQTT_CONNECT_TIMEOUT", "MIN_VOLUME_EUR",
    "AI_CALL_DELAY_SECONDS", "AI_ACCURACY_HORIZON_HOURS", "SENTIMENT_VETO_CONF",
    "WIN_EXCL_COOLDOWN_HOURS", "MIN_ORDER_EUR",
}


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
            # Normalise comma decimal separator to dot before persisting
            if key in _NUMERIC_KEYS and value:
                value = value.replace(",", ".")
            updates[key] = value
    return updates
