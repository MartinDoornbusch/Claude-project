# Bitvavo AI Trading Bot

Automatische trading en portfolio app voor Bitvavo.

## Fase 1 — Read-only data & indicatoren

### Installatie

```bash
pip install -r requirements.txt
```

### API sleutels instellen

```bash
cp .env.example .env
# Vul je Bitvavo API key en secret in (alleen 'Lezen' rechten nodig)
```

### Gebruik

**Portfolio bekijken:**
```bash
python main.py portfolio
```

**Candle data + indicatoren voor een handelspaar:**
```bash
python main.py candles BTC-EUR
python main.py candles ETH-EUR --interval 4h
```

Beschikbare intervallen: `1m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d`

### Indicatoren (Fase 1)

| Indicator | Beschrijving |
|---|---|
| SMA 20 / SMA 50 | Korte en lange moving average (trend) |
| RSI 14 | Momentum — boven 70 = overbought, onder 30 = oversold |
| MACD | Trendkracht en richtingsverandering |
| Bollinger Bands | Volatiliteitsbanden rondom de prijs |

### Roadmap

- **Fase 2** — Paper trading (signalen loggen zonder echte trades)
- **Fase 3** — Echte trades met limieten (klein budget)
- **Fase 4** — Dashboard + Claude AI integratie
