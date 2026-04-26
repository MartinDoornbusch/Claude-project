"""MQTT publisher — stuurt data naar Home Assistant via MQTT discovery."""

from __future__ import annotations

import json
import logging
import os
import socket

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

HA_DISCOVERY_PREFIX = "homeassistant"


def _make_client(user: str, password: str) -> mqtt.Client:
    client = mqtt.Client(client_id="bitvavo-bot", clean_session=True)
    if user:
        client.username_pw_set(user, password)
    return client


def _publish(client: mqtt.Client, topic: str, payload: str | dict, retain: bool = True) -> None:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    result = client.publish(topic, payload, qos=1, retain=retain)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning("MQTT publish mislukt op %s (rc=%d)", topic, result.rc)


def _register_sensor(
    client: mqtt.Client,
    prefix: str,
    sensor_id: str,
    name: str,
    unit: str,
    device_class: str | None = None,
    icon: str | None = None,
) -> str:
    state_topic  = f"{prefix}/sensor/{sensor_id}/state"
    config_topic = f"{HA_DISCOVERY_PREFIX}/sensor/{prefix}_{sensor_id}/config"

    config: dict = {
        "name": name,
        "unique_id": f"{prefix}_{sensor_id}",
        "state_topic": state_topic,
        "unit_of_measurement": unit,
        "device": {
            "identifiers": [prefix],
            "name": "Bitvavo Trading Bot",
            "model": "AI Trading Bot",
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
    # Lees instellingen dynamisch zodat wijzigingen zonder herstart actief zijn
    mqtt_enabled = os.getenv("MQTT_ENABLED", "true").lower()
    if mqtt_enabled == "false":
        return

    host    = os.getenv("MQTT_HOST", "").strip()
    port    = int(os.getenv("MQTT_PORT", "1883"))
    user    = os.getenv("MQTT_USER", "")
    passwd  = os.getenv("MQTT_PASS", "")
    prefix  = os.getenv("MQTT_PREFIX", "bitvavo")
    timeout = int(os.getenv("MQTT_CONNECT_TIMEOUT", "3"))  # seconden

    if not host:
        return

    client = _make_client(user, passwd)

    # Gebruik een korte socket-timeout zodat de bot niet 30+ sec blokkeert
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        client.connect(host, port, keepalive=30)
        socket.setdefaulttimeout(old_timeout)
        client.loop_start()

        # Portfolio totaal
        total_topic = _register_sensor(
            client, prefix, "portfolio_total", "Portfolio waarde", "EUR",
            device_class="monetary", icon="mdi:wallet"
        )
        _publish(client, total_topic, f"{portfolio['total_eur']:.2f}")

        # Cash
        cash_topic = _register_sensor(
            client, prefix, "portfolio_cash", "Cash (EUR)", "EUR",
            device_class="monetary", icon="mdi:cash"
        )
        _publish(client, cash_topic, f"{portfolio['cash_eur']:.2f}")

        # Per markt
        for market, sig in market_signals.items():
            safe = market.replace("-", "_").lower()

            price_topic = _register_sensor(
                client, prefix, f"{safe}_price", f"{market} prijs", "EUR",
                device_class="monetary", icon="mdi:chart-line"
            )
            _publish(client, price_topic, f"{sig['close']:.4f}")

            if sig.get("rsi_14") is not None:
                rsi_topic = _register_sensor(
                    client, prefix, f"{safe}_rsi", f"{market} RSI", "",
                    icon="mdi:gauge"
                )
                _publish(client, rsi_topic, f"{sig['rsi_14']:.2f}")

            signal_topic = _register_sensor(
                client, prefix, f"{safe}_signal", f"{market} signaal", "",
                icon="mdi:signal"
            )
            _publish(client, signal_topic, sig.get("signal", "HOLD"))

            pos = portfolio["positions"].get(market)
            if pos:
                pnl_topic = _register_sensor(
                    client, prefix, f"{safe}_pnl", f"{market} paper PnL", "EUR",
                    device_class="monetary", icon="mdi:trending-up"
                )
                _publish(client, pnl_topic, f"{pos['pnl']:.2f}")

        client.loop_stop()
        client.disconnect()
        logger.debug("MQTT gepubliceerd naar %s:%d", host, port)

    except OSError as exc:
        socket.setdefaulttimeout(old_timeout)
        logger.warning(
            "MQTT niet beschikbaar: %s — zet MQTT_ENABLED=false in Instellingen als je geen broker gebruikt", exc
        )
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
