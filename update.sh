#!/bin/sh
# Update script — haalt laatste code op en kopieert add-on bestanden
set -e

REPO_DIR="/config/bitvavo-bot"
ADDON_DIR="/addons/bitvavo-bot"

echo "[update] Code ophalen van GitHub..."
cd "$REPO_DIR"
git pull origin claude/bitvavo-ai-trading-bot-VI0ln

echo "[update] Add-on bestanden kopiëren naar $ADDON_DIR..."
cp "$REPO_DIR/ha-addon/config.yaml"  "$ADDON_DIR/config.yaml"
cp "$REPO_DIR/ha-addon/Dockerfile"   "$ADDON_DIR/Dockerfile"
cp "$REPO_DIR/ha-addon/run.sh"       "$ADDON_DIR/run.sh"

echo "[update] Add-on herladen..."
ha addons reload

echo ""
echo "✓ Klaar! Ga naar HA → Add-ons → Bitvavo Trading Bot"
echo "  Als er een nieuwe versie staat → klik Bijwerken (herbouwt de image)"
echo "  Anders → klik Herstarten om de nieuwe code te activeren"
