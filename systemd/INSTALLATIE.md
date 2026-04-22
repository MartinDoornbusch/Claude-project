# Installatie op Raspberry Pi

## 1. Project kopiëren naar de Pi

```bash
# Op je Pi:
git clone https://github.com/MartinDoornbusch/Claude-project.git /home/pi/bitvavo-bot
cd /home/pi/bitvavo-bot
```

## 2. Python virtual environment aanmaken

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. .env instellen

```bash
cp .env.example .env
nano .env   # Vul je API keys en MQTT gegevens in
```

## 4. Systemd service installeren

```bash
sudo cp systemd/bitvavo-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bitvavo-bot
sudo systemctl start bitvavo-bot
```

## 5. Logs bekijken

```bash
sudo journalctl -u bitvavo-bot -f
```

## 6. Status controleren

```bash
sudo systemctl status bitvavo-bot
```

## 7. Stoppen / herstarten

```bash
sudo systemctl stop bitvavo-bot
sudo systemctl restart bitvavo-bot
```

## Home Assistant MQTT

Zorg dat de MQTT-integratie actief is in Home Assistant:
- **Instellingen → Apparaten & diensten → MQTT**
- Sensoren verschijnen automatisch via MQTT discovery onder apparaat **"Bitvavo Trading Bot"**

Standaard sensoren:
| Sensor | Beschrijving |
|---|---|
| Portfolio waarde | Totale paper portfolio waarde in EUR |
| Cash (EUR) | Beschikbaar virtueel cash |
| BTC-EUR prijs | Actuele BTC prijs |
| BTC-EUR RSI | RSI indicator |
| BTC-EUR signaal | BUY / SELL / HOLD |
| BTC-EUR paper PnL | Ongerealiseerde winst/verlies |
