#!/bin/sh
set -e

cd /config/bitvavo-bot

# Maak venv aan als die er nog niet is (eerste keer).
if [ ! -f venv/bin/activate ]; then
    echo "[addon] Venv aanmaken en dependencies installeren..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

. venv/bin/activate

echo "[addon] Trading bot starten..."
python3 main.py run &

echo "[addon] Web dashboard starten op poort 5000..."
exec python3 main.py web --port 5001
