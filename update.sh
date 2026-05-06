#!/bin/sh
# Update script — haalt laatste code op en kopieert add-on bestanden
set -e

REPO_DIR="/config/bitvavo-bot"
ADDON_DIR="/addons/bitvavo-bot"

echo "[update] Code ophalen van GitHub..."
cd "$REPO_DIR"
git fetch origin main
git reset --hard origin/main

echo "[update] Add-on bestanden kopiëren naar $ADDON_DIR..."
cp "$REPO_DIR/ha-addon/config.yaml"    "$ADDON_DIR/config.yaml"
cp "$REPO_DIR/ha-addon/Dockerfile"     "$ADDON_DIR/Dockerfile"
cp "$REPO_DIR/ha-addon/run.sh"         "$ADDON_DIR/run.sh"
cp "$REPO_DIR/requirements.txt"        "$ADDON_DIR/requirements.txt"
cp "$REPO_DIR/ha-addon/CHANGELOG.md"   "$ADDON_DIR/CHANGELOG.md"

echo "[update] Add-on herladen..."
ha addons reload

echo "[update] Supervisor herstarten zodat nieuwe versie zichtbaar wordt..."
ha supervisor restart
