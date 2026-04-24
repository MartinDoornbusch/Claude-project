#!/bin/sh
# Startscript voor de Bitvavo trading bot op Home Assistant OS (Alpine Linux).
# Python en venv worden automatisch opgebouwd; de venv in /config/ blijft bewaard.
cd /config/bitvavo-bot

# Python 3 ontbreekt na herstart van de Terminal & SSH add-on — installeer opnieuw.
REINSTALLED=0
if ! command -v python3 > /dev/null 2>&1; then
    echo "[start.sh] Python3 niet gevonden — installeren via apk..."
    apk update -q && apk add -q python3 py3-pip
    REINSTALLED=1
fi

# Maak (of hermaak na herinstallatie) de venv aan en installeer dependencies.
if [ "$REINSTALLED" = "1" ] || [ ! -f venv/bin/activate ]; then
    echo "[start.sh] Venv aanmaken en dependencies installeren (even geduld)..."
    rm -rf venv
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

echo "[start.sh] Bot starten..."
. venv/bin/activate
exec python3 main.py bot
