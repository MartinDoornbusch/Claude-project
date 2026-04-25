#!/bin/sh
set -e

cd /config/bitvavo-bot

echo "[addon] Code bijwerken van GitHub (main)..."
git fetch origin main 2>/dev/null || echo "[addon] Waarschuwing: git fetch mislukt, ga door met bestaande code."
git reset --hard origin/main 2>/dev/null || echo "[addon] Waarschuwing: git reset mislukt, ga door met bestaande code."

echo "[addon] Trading bot starten..."
python3 main.py run &

echo "[addon] Web dashboard starten op poort 5001..."
exec python3 main.py web --port 5001
