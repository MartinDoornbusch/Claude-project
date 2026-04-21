"""MQTT publisher — stuurt data naar Home Assistant via MQTT discovery."""

from __future__ import annotations

import json
import logging
import os

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "bitvavo")
HA_DISCOVERY_PREFIX = "homeassistant"


def _make_client() -> mqtt.Client:
    client = mqtt.Client(client_id="bitvavo-bot", clean_session=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    return client


def _publish(client: mqtt.Client, topic: str, payload: str | dict, retain: bool = True) -> None:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    result = client.publish(topic, payload, qos=1, retain=retain)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning("MQTT publish mislukt op %s (rc=%d)", topic, result.rc)


def _register_sensor(
    client: mqtt.Client,
    sensor_id: str,
    name: str,
    unit: str,
    device_class: str | None = None,
    icon: str | None = None,
) -> str:
    """Registreer een sensor via HA MQTT discovery. Retourneert het state topic."""
    state_topic = f"{MQTT_PREFIX}/sensor/{sensor_id}/state"
    config_topic = f"{HA_DISCOVERY_PREFIX}/sensor/{MQTT_PREFIX}_{sensor_id}/config"

    config: dict = {
        "name": name,
        "unique_id": f"{MQTT_PREFIX}_{sensor_id}",
        "state_topic": state_topic,
        "unit_of_measurement": unit,
        "device": {
            "identifiers": [MQTT_PREFIX],
            "name": "Bitvavo Trading Bot",
            "model": "Fase 2 — Paper Trading",
            "manufacturer": "DIY",
        },
    }
    if device_class:
        config["device_class"] = device_class
    if icon:
        config["icon"] = icon

    _publish(client, config_topic, config)
    return state_topic


def publish_all(portfolio: dict, market_signals: dict[str, dict]) -> None:
    """
    Publiceer portfolio en signalen naar Home Assistant.

    portfolio      — output van paper_trader.portfolio_value()
    market_signals — {"BTC-EUR": {"close": ..., "rsi_14": ..., "signal": ...}, ...}
    """
    if not MQTT_HOST:
        return

    client = _make_client()
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()

        # Portfolio totaal
        total_topic = _register_sensor(
            client, "portfolio_total", "Portfolio waarde", "EUR",
            device_class="monetary", icon="mdi:wallet"
        )
        _publish(client, total_topic, f"{portfolio['total_eur']:.2f}")

        # Cash
        cash_topic = _register_sensor(
            client, "portfolio_cash", "Cash (EUR)", "EUR",
            device_class="monetary", icon="mdi:cash"
        )
        _publish(client, cash_topic, f"{portfolio['cash_eur']:.2f}")

        # Per markt
        for market, sig in market_signals.items():
            safe = market.replace("-", "_").lower()

            price_topic = _register_sensor(
                client, f"{safe}_price", f"{market} prijs", "EUR",
                device_class="monetary", icon="mdi:chart-line"
            )
            _publish(client, price_topic, f"{sig['close']:.4f}")

            if sig.get("rsi_14") is not None:
                rsi_topic = _register_sensor(
                    client, f"{safe}_rsi", f"{market} RSI", "",
                    icon="mdi:gauge"
                )
                _publish(client, rsi_topic, f"{sig['rsi_14']:.2f}")

            signal_topic = _register_sensor(
                client, f"{safe}_signal", f"{market} signaal", "",
                icon="mdi:signal"
            )
            _publish(client, signal_topic, sig.get("signal", "HOLD"))

            # Positie PnL indien open
            pos = portfolio["positions"].get(market)
            if pos:
                pnl_topic = _register_sensor(
                    client, f"{safe}_pnl", f"{market} paper PnL", "EUR",
                    device_class="monetary", icon="mdi:trending-up"
                )
                _publish(client, pnl_topic, f"{pos['pnl']:.2f}")

        client.loop_stop()
        client.disconnect()
        logger.debug("MQTT gepubliceerd naar %s:%d", MQTT_HOST, MQTT_PORT)

    except Exception as exc:
        logger.warning("MQTT niet beschikbaar: %s", exc)
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
