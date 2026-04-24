#!/bin/sh
# Startscript voor de Bitvavo trading bot op Home Assistant OS (Alpine Linux).
# Draait de scheduler (run) in de achtergrond en het web dashboard op de voorgrond.
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

. venv/bin/activate

echo "[start.sh] Trading bot starten (achtergrond)..."
python3 main.py run &
BOT_PID=$!

# Zorg dat de bot ook stopt als het dashboard wordt afgesloten.
trap "kill $BOT_PID 2>/dev/null; exit 0" INT TERM EXIT

echo "[start.sh] Web dashboard starten op poort 5000..."
exec python3 main.py web
