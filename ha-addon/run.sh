#!/bin/sh
set -e

cd /config/bitvavo-bot

echo "[addon] Trading bot starten..."
python3 main.py run &

echo "[addon] Web dashboard starten op poort 5001..."
exec python3 main.py web --port 5001
