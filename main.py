"""Bitvavo AI Trading Bot — Fase 1: read-only data & indicatoren."""

from __future__ import annotations

import argparse
import sys

from src.bitvavo_client import get_client
from src.portfolio import get_portfolio_value_eur
from src.candles import get_candles, add_indicators, latest_signals


def cmd_portfolio(_args) -> None:
    client = get_client()
    balances, total = get_portfolio_value_eur(client)

    print("\n=== Portfolio ===")
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
        label = " ⚠ OVERBOUGHT" if rsi > 70 else (" ⚠ OVERSOLD" if rsi < 30 else "")
        print(f"  RSI 14:       {rsi:.2f}{label}")
    if signals["macd"] is not None:
        print(f"  MACD:         {signals['macd']:.6f}  (signaal: {signals['macd_signal']:.6f})")
    if signals["bb_lower"] is not None:
        print(f"  Bollinger:    €{signals['bb_lower']:.4f} — €{signals['bb_upper']:.4f}")
    if signals["ma_cross"]:
        cross_label = "GOLDEN CROSS (bullish)" if signals["ma_cross"] == "golden_cross" else "DEATH CROSS (bearish)"
        print(f"\n  *** MA Signaal: {cross_label} ***")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bitvavo AI Trading Bot — Fase 1"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("portfolio", help="Toon je portfolio en EUR-waarde")

    candles_parser = sub.add_parser("candles", help="Toon candle data en indicatoren")
    candles_parser.add_argument("market", help="Handelspaar, bijv. BTC-EUR")
    candles_parser.add_argument(
        "--interval", default="1h",
        choices=["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"],
        help="Candle interval (standaard: 1h)"
    )

    args = parser.parse_args()

    try:
        if args.command == "portfolio":
            cmd_portfolio(args)
        elif args.command == "candles":
            cmd_candles(args)
    except EnvironmentError as e:
        print(f"\nFout: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"\nAPI fout: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
