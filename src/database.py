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

            CREATE TABLE IF NOT EXISTS ai_decisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market      TEXT NOT NULL,
                decision    TEXT NOT NULL,
                confidence  REAL NOT NULL,
                reasoning   TEXT,
                executed    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS market_watchlist (
                market          TEXT PRIMARY KEY,
                enabled         INTEGER NOT NULL DEFAULT 0,
                ai_recommended  INTEGER NOT NULL DEFAULT 0,
                ai_confidence   REAL,
                ai_reasoning    TEXT,
                last_advised    TEXT,
                last_price      REAL,
                change_24h      REAL,
                volume_eur      REAL,
                last_scanned    TEXT
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


# --- AI decisions ---

def save_ai_decision(
    market: str, decision: str, confidence: float, reasoning: str, executed: bool = False
) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO ai_decisions (ts, market, decision, confidence, reasoning, executed)
            VALUES (?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            market, decision, confidence, reasoning, 1 if executed else 0,
        ))
        return cur.lastrowid


def get_ai_decisions(market: str | None = None, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM ai_decisions WHERE market=? ORDER BY ts DESC LIMIT ?",
                (market, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_decisions ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_ai_decisions_today(market: str) -> int:
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM ai_decisions "
            "WHERE market=? AND DATE(ts)=? AND executed=1",
            (market, today)
        ).fetchone()
    return row["cnt"] if row else 0


# --- Market watchlist ---

def get_watchlist() -> list[dict]:
    """Retourneert alle markten in de watchlist, gesorteerd op volume."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM market_watchlist ORDER BY volume_eur DESC NULLS LAST"
        ).fetchall()
    return [dict(r) for r in rows]


def get_enabled_markets() -> list[str]:
    """Retourneert de lijst van ingeschakelde markten."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT market FROM market_watchlist WHERE enabled=1 ORDER BY volume_eur DESC NULLS LAST"
        ).fetchall()
    return [r["market"] for r in rows]


def set_market_enabled(market: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO market_watchlist (market, enabled)
            VALUES (?, ?)
            ON CONFLICT(market) DO UPDATE SET enabled=excluded.enabled
        """, (market, 1 if enabled else 0))


def upsert_market_stats(market: str, price: float, change_24h: float, volume_eur: float) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO market_watchlist (market, last_price, change_24h, volume_eur, last_scanned)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(market) DO UPDATE SET
                last_price=excluded.last_price,
                change_24h=excluded.change_24h,
                volume_eur=excluded.volume_eur,
                last_scanned=excluded.last_scanned
        """, (market, price, change_24h, volume_eur, now))


# --- Analytics ---

def get_all_paper_trades_asc(market: str | None = None) -> list[dict]:
    """Alle paper trades chronologisch, voor PnL-pairing."""
    with get_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE market=? ORDER BY ts ASC",
                (market,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY ts ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_daily_pnl_series() -> list[dict]:
    """Geaggregeerde daily PnL uit de daily_pnl tabel."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, SUM(realized_eur) AS pnl FROM daily_pnl GROUP BY date ORDER BY date ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def save_market_advice(market: str, recommended: bool, confidence: float | None, reasoning: str) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO market_watchlist (market, ai_recommended, ai_confidence, ai_reasoning, last_advised)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(market) DO UPDATE SET
                ai_recommended=excluded.ai_recommended,
                ai_confidence=excluded.ai_confidence,
                ai_reasoning=excluded.ai_reasoning,
                last_advised=excluded.last_advised
        """, (market, 1 if recommended else 0, confidence, reasoning, now))
