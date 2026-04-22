"""SQLite database — initialisatie en CRUD voor signalen en paper trades."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "trading.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market      TEXT NOT NULL,
                interval    TEXT NOT NULL,
                close       REAL NOT NULL,
                sma_20      REAL,
                sma_50      REAL,
                rsi_14      REAL,
                macd        REAL,
                macd_signal REAL,
                bb_lower    REAL,
                bb_upper    REAL,
                signal      TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market      TEXT NOT NULL,
                side        TEXT NOT NULL,
                price       REAL NOT NULL,
                amount      REAL NOT NULL,
                eur_total   REAL NOT NULL,
                reason      TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_portfolio (
                market      TEXT PRIMARY KEY,
                amount      REAL NOT NULL DEFAULT 0,
                avg_price   REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS paper_cash (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                eur         REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS live_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market      TEXT NOT NULL,
                side        TEXT NOT NULL,
                order_id    TEXT,
                price       REAL,
                amount      REAL,
                eur_total   REAL,
                status      TEXT NOT NULL DEFAULT 'pending',
                reason      TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date        TEXT NOT NULL,
                market      TEXT NOT NULL,
                realized_eur REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (date, market)
            );
        """)


# --- Signals ---

def save_signal(market: str, interval: str, indicators: dict, signal: str | None) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO signals (ts, market, interval, close, sma_20, sma_50,
                                 rsi_14, macd, macd_signal, bb_lower, bb_upper, signal)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            market, interval,
            indicators.get("close"),
            indicators.get("sma_20"),
            indicators.get("sma_50"),
            indicators.get("rsi_14"),
            indicators.get("macd"),
            indicators.get("macd_signal"),
            indicators.get("bb_lower"),
            indicators.get("bb_upper"),
            signal,
        ))


def get_latest_signals(market: str, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE market=? ORDER BY ts DESC LIMIT ?",
            (market, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Paper portfolio ---

def get_cash(starting_capital: float = 1000.0) -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT eur FROM paper_cash WHERE id=1").fetchone()
        if row is None:
            conn.execute("INSERT INTO paper_cash (id, eur) VALUES (1, ?)", (starting_capital,))
            return starting_capital
        return row["eur"]


def set_cash(eur: float) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO paper_cash (id, eur) VALUES (1, ?)", (eur,))


def get_position(market: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT amount, avg_price FROM paper_portfolio WHERE market=?", (market,)
        ).fetchone()
    return dict(row) if row else {"amount": 0.0, "avg_price": 0.0}


def set_position(market: str, amount: float, avg_price: float) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO paper_portfolio (market, amount, avg_price)
            VALUES (?,?,?)
        """, (market, amount, avg_price))


def save_paper_trade(market: str, side: str, price: float, amount: float, reason: str = "") -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO paper_trades (ts, market, side, price, amount, eur_total, reason)
            VALUES (?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            market, side, price, amount, price * amount, reason,
        ))


def get_paper_trades(market: str | None = None, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE market=? ORDER BY ts DESC LIMIT ?",
                (market, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# --- Live trades ---

def save_live_trade(
    market: str, side: str, order_id: str | None,
    price: float | None, amount: float | None,
    eur_total: float | None, status: str = "pending", reason: str = ""
) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO live_trades (ts, market, side, order_id, price, amount, eur_total, status, reason)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            market, side, order_id, price, amount, eur_total, status, reason,
        ))
        return cur.lastrowid


def update_live_trade(trade_id: int, price: float, amount: float, eur_total: float, status: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            UPDATE live_trades SET price=?, amount=?, eur_total=?, status=? WHERE id=?
        """, (price, amount, eur_total, status, trade_id))


def get_live_trades(market: str | None = None, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM live_trades WHERE market=? ORDER BY ts DESC LIMIT ?",
                (market, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM live_trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_daily_loss(market: str) -> float:
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT realized_eur FROM daily_pnl WHERE date=? AND market=?",
            (today, market)
        ).fetchone()
    return row["realized_eur"] if row else 0.0


def add_daily_pnl(market: str, pnl_eur: float) -> None:
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO daily_pnl (date, market, realized_eur) VALUES (?,?,?)
            ON CONFLICT(date, market) DO UPDATE SET realized_eur = realized_eur + ?
        """, (today, market, pnl_eur, pnl_eur))
