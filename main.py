"""Bitvavo AI Trading Bot."""

from __future__ import annotations

import argparse
import os
import sys

from src.bitvavo_client import get_client
from src.portfolio import get_portfolio_value_eur
from src.candles import get_candles, add_indicators, latest_signals
from src.database import (
    init_db, get_paper_trades, get_latest_signals,
    get_cash, get_position, get_live_trades, get_daily_loss,
)
from src.paper_trader import portfolio_value


def cmd_portfolio(_args) -> None:
    client = get_client()
    balances, total = get_portfolio_value_eur(client)

    print("\n=== Portfolio (Bitvavo) ===")
    print(f"{'Munt':<8} {'Beschikbaar':>14} {'In order':>12} {'EUR waarde':>12}")
    print("-" * 52)
    for b in balances:
        print(
            f"{b['symbol']:<8} "
            f"{b['available']:>14.6f} "
            f"{b['inOrder']:>12.6f} "
            f"€{b['eurValue']:>11.2f}"
        )
    print("-" * 52)
    print(f"{'Totaal':<8} {'':>14} {'':>12} €{total:>11.2f}\n")


def cmd_candles(args) -> None:
    client = get_client()
    market = args.market.upper()
    interval = args.interval

    print(f"\n=== Candles: {market} ({interval}) ===")
    df = get_candles(client, market, interval, limit=200)
    df = add_indicators(df)
    signals = latest_signals(df)

    print(f"Laatste candle: {df.iloc[-1]['timestamp']}")
    print(f"  Prijs:        €{signals['close']:.4f}")

    if signals["sma_20"] is not None:
        print(f"  SMA 20:       €{signals['sma_20']:.4f}")
    if signals["sma_50"] is not None:
        print(f"  SMA 50:       €{signals['sma_50']:.4f}")
    if signals["rsi_14"] is not None:
        rsi = signals["rsi_14"]
        label = " OVERBOUGHT" if rsi > 70 else (" OVERSOLD" if rsi < 30 else "")
        print(f"  RSI 14:       {rsi:.2f}{label}")
    if signals["macd"] is not None:
        print(f"  MACD:         {signals['macd']:.6f}  (signaal: {signals['macd_signal']:.6f})")
    if signals["bb_lower"] is not None:
        print(f"  Bollinger:    €{signals['bb_lower']:.4f} — €{signals['bb_upper']:.4f}")
    if signals["ma_cross"]:
        cross_label = "GOLDEN CROSS (bullish)" if signals["ma_cross"] == "golden_cross" else "DEATH CROSS (bearish)"
        print(f"\n  *** MA Signaal: {cross_label} ***")
    print()


def cmd_run(_args) -> None:
    from src.scheduler import start
    start()


def cmd_web(args) -> None:
    from src.web_dashboard import start
    port = int(args.port)
    print(f"\nWeb dashboard gestart op http://0.0.0.0:{port}")
    print("Bereikbaar via http://192.168.178.80:{port} op je netwerk\n")
    start(port=port)


def cmd_paper_status(args) -> None:
    init_db()
    import os
    markets = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]

    client = get_client()
    prices = {}
    for market in markets:
        from src.portfolio import get_ticker_price
        p = get_ticker_price(client, market)
        if p:
            prices[market] = p

    pf = portfolio_value(prices)

    print("\n=== Paper Portfolio ===")
    print(f"  Cash:         €{pf['cash_eur']:.2f}")
    for market, pos in pf["positions"].items():
        print(f"\n  {market}:")
        print(f"    Hoeveelheid:  {pos['amount']:.6f}")
        print(f"    Gem. prijs:   €{pos['avg_price']:.4f}")
        print(f"    Huidige prijs:€{pos['current_price']:.4f}")
        print(f"    EUR waarde:   €{pos['eur_value']:.2f}")
        print(f"    PnL:          €{pos['pnl']:+.2f}")
    print(f"\n  Totaal:       €{pf['total_eur']:.2f}\n")

    print("=== Laatste signalen ===")
    for market in markets:
        sigs = get_latest_signals(market, limit=5)
        print(f"\n  {market}:")
        for s in sigs:
            print(f"    {s['ts'][:16]}  {s['signal']:<5}  prijs: €{s['close']:.4f}  RSI: {s['rsi_14'] or '-'}")

    print("\n=== Laatste trades ===")
    trades = get_paper_trades(limit=10)
    if trades:
        for t in trades:
            print(f"  {t['ts'][:16]}  {t['market']}  {t['side']:<4}  "
                  f"prijs: €{t['price']:.4f}  bedrag: {t['amount']:.6f}  "
                  f"€{t['eur_total']:.2f}")
    else:
        print("  Nog geen trades uitgevoerd.")
    print()


def cmd_live_status(_args) -> None:
    init_db()
    live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
    markets = [m.strip() for m in os.getenv("TRADING_MARKETS", "BTC-EUR").split(",")]

    print("\n=== Live trading status ===")
    print(f"  Modus:              {'LIVE' if live_enabled else 'PAPER (live uitgeschakeld)'}")
    print(f"  MAX_TRADE_EUR:      €{os.getenv('MAX_TRADE_EUR', '25')}")
    print(f"  MAX_EXPOSURE_EUR:   €{os.getenv('MAX_EXPOSURE_EUR', '100')}")
    print(f"  DAILY_LOSS_LIMIT:   €{os.getenv('DAILY_LOSS_LIMIT_EUR', '50')}")

    print("\n=== Dagelijks verlies ===")
    for market in markets:
        loss = get_daily_loss(market)
        print(f"  {market}: €{loss:+.2f}")

    print("\n=== Laatste live orders ===")
    trades = get_live_trades(limit=10)
    if trades:
        for t in trades:
            print(f"  {t['ts'][:16]}  {t['market']}  {t['side']:<4}  "
                  f"status: {t['status']:<8}  "
                  f"prijs: €{t['price'] or 0:.4f}  €{t['eur_total'] or 0:.2f}")
    else:
        print("  Nog geen live orders geplaatst.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bitvavo AI Trading Bot"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("portfolio", help="Toon je Bitvavo portfolio en EUR-waarde")

    candles_parser = sub.add_parser("candles", help="Toon candle data en indicatoren")
    candles_parser.add_argument("market", help="Handelspaar, bijv. BTC-EUR")
    candles_parser.add_argument(
        "--interval", default="1h",
        choices=["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"],
        help="Candle interval (standaard: 1h)"
    )

    sub.add_parser("run", help="Start de bot (paper of live, afhankelijk van .env)")
    sub.add_parser("status", help="Toon paper portfolio, signalen en trades")
    sub.add_parser("live-status", help="Toon live trading status en orders")

    web_parser = sub.add_parser("web", help="Start het web dashboard")
    web_parser.add_argument("--port", default="5000", help="Poort (standaard: 5000)")

    args = parser.parse_args()

    try:
        if args.command == "portfolio":
            cmd_portfolio(args)
        elif args.command == "candles":
            cmd_candles(args)
        elif args.command == "run":
            cmd_run(args)
        elif args.command == "status":
            cmd_paper_status(args)
        elif args.command == "live-status":
            cmd_live_status(args)
        elif args.command == "web":
            cmd_web(args)
    except EnvironmentError as e:
        print(f"\nFout: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"\nAPI fout: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
