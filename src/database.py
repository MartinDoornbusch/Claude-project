"""SQLite database — initialisatie en CRUD voor signalen en paper trades."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent.parent / "trading.db"

_AMS = ZoneInfo("Europe/Amsterdam")


def _now() -> str:
    """Huidige Amsterdam-tijd als ISO-string (inclusief offset)."""
    return datetime.now(_AMS).isoformat(timespec="seconds")


def _today() -> str:
    """Huidige datum in de Amsterdam-tijdzone."""
    return datetime.now(_AMS).date().isoformat()


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
            PRAGMA journal_mode=WAL;

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

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                cash_eur  REAL NOT NULL,
                pos_eur   REAL NOT NULL,
                total_eur REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS position_meta (
                market                TEXT PRIMARY KEY,
                peak_price            REAL NOT NULL DEFAULT 0,
                breakeven_set         INTEGER NOT NULL DEFAULT 0,
                house_money_activated INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backtest_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market      TEXT NOT NULL,
                interval    TEXT NOT NULL,
                sma_short   INTEGER,
                sma_long    INTEGER,
                rsi_buy     REAL,
                rsi_sell    REAL,
                capital     REAL,
                return_pct  REAL,
                sharpe      REAL,
                max_dd      REAL,
                win_rate    REAL,
                num_trades  INTEGER
            );

            CREATE TABLE IF NOT EXISTS oco_orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                market       TEXT NOT NULL,
                amount       REAL NOT NULL,
                tp_order_id  TEXT,
                sl_order_id  TEXT,
                tp_price     REAL,
                sl_price     REAL,
                status       TEXT NOT NULL DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS groq_token_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                date        TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS google_request_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                date        TEXT NOT NULL,
                requests    INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS mistral_token_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                date        TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cerebras_token_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                date        TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ai_accuracy (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL UNIQUE,
                market      TEXT NOT NULL,
                decision    TEXT NOT NULL,
                confidence  REAL NOT NULL,
                entry_price REAL NOT NULL,
                eval_price  REAL,
                pnl_pct     REAL,
                outcome     TEXT,
                horizon_h   REAL NOT NULL,
                eval_ts     TEXT
            );
        """)
    # Migration: add columns to existing tables if missing
    with get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(position_meta)").fetchall()}
        if "house_money_activated" not in cols:
            conn.execute(
                "ALTER TABLE position_meta ADD COLUMN house_money_activated INTEGER NOT NULL DEFAULT 0"
            )
        trade_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        if "planned_price" not in trade_cols:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN planned_price REAL")
        if "fee" not in trade_cols:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN fee REAL NOT NULL DEFAULT 0")
        sig_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "atr_14" not in sig_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN atr_14 REAL")
        ai_cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
        if "entry_price" not in ai_cols:
            conn.execute("ALTER TABLE ai_decisions ADD COLUMN entry_price REAL")


# --- Signals ---

def save_signal(market: str, interval: str, indicators: dict, signal: str | None) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO signals (ts, market, interval, close, sma_20, sma_50,
                                 rsi_14, macd, macd_signal, bb_lower, bb_upper, signal, atr_14)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            _now(),
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
            indicators.get("atr_14"),
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


def save_paper_trade(
    market: str, side: str, price: float, amount: float,
    reason: str = "", planned_price: float | None = None, fee: float = 0.0,
) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO paper_trades (ts, market, side, price, amount, eur_total, reason, planned_price, fee)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            _now(),
            market, side, price, amount, price * amount, reason, planned_price, fee,
        ))


def get_total_fees_paid() -> float:
    """Totaal betaalde transactiekosten over alle paper trades."""
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(fee), 0) FROM paper_trades").fetchone()
        return float(row[0])


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
            _now(),
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
    today = _today()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT realized_eur FROM daily_pnl WHERE date=? AND market=?",
            (today, market)
        ).fetchone()
    return row["realized_eur"] if row else 0.0


def get_total_daily_loss() -> float:
    """Retourneert het gerealiseerde PnL van vandaag over alle markten (negatief = verlies)."""
    today = _today()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_eur), 0) AS total FROM daily_pnl WHERE date=?",
            (today,)
        ).fetchone()
    return row["total"] if row else 0.0


def get_latest_portfolio_total() -> float:
    """Retourneert de meest recente portfolio-totaalwaarde uit snapshots, of 0.0 als er nog geen is."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT total_eur FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return row["total_eur"] if row else 0.0


def add_daily_pnl(market: str, pnl_eur: float) -> None:
    today = _today()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO daily_pnl (date, market, realized_eur) VALUES (?,?,?)
            ON CONFLICT(date, market) DO UPDATE SET realized_eur = realized_eur + ?
        """, (today, market, pnl_eur, pnl_eur))


# --- AI decisions ---

def save_ai_decision(
    market: str, decision: str, confidence: float, reasoning: str,
    executed: bool = False, entry_price: float = 0.0,
) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO ai_decisions (ts, market, decision, confidence, reasoning, executed, entry_price)
            VALUES (?,?,?,?,?,?,?)
        """, (
            _now(),
            market, decision, confidence, reasoning, 1 if executed else 0, entry_price,
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
    today = _today()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM ai_decisions "
            "WHERE market=? AND DATE(ts)=? AND executed=1",
            (market, today)
        ).fetchone()
    return row["cnt"] if row else 0


def mark_ai_decision_executed(decision_id: int) -> None:
    """Markeer een AI-beslissing als daadwerkelijk uitgevoerd (na echte fill)."""
    with get_conn() as conn:
        conn.execute("UPDATE ai_decisions SET executed=1 WHERE id=?", (decision_id,))


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
    now = _now()
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


# --- Analytics & AI context helpers ---

def get_last_buy_ts(market: str) -> str | None:
    """Tijdstip van de meest recente BUY paper trade voor deze markt."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ts FROM paper_trades WHERE market=? AND side='BUY' ORDER BY ts DESC LIMIT 1",
            (market,)
        ).fetchone()
    return row["ts"] if row else None


def get_recent_trade_pairs(market: str, limit: int = 5) -> list[dict]:
    """
    Retourneert de laatste N afgesloten BUY→SELL paren voor deze markt.
    Gebruikt FIFO-matching op chronologische volgorde.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE market=? ORDER BY ts ASC",
            (market,)
        ).fetchall()

    trades = [dict(r) for r in rows]
    open_buys: list[dict] = []
    pairs: list[dict] = []

    for t in trades:
        if t["side"] == "BUY":
            open_buys.append(t)
        elif t["side"] == "SELL" and open_buys:
            buy = open_buys.pop(0)
            pnl_eur = t["eur_total"] - buy["eur_total"]
            pnl_pct = (t["price"] - buy["price"]) / buy["price"] * 100 if buy["price"] else 0
            pairs.append({
                "buy_ts":    buy["ts"],
                "sell_ts":   t["ts"],
                "buy_price": buy["price"],
                "sell_price": t["price"],
                "pnl_eur":   round(pnl_eur, 4),
                "pnl_pct":   round(pnl_pct, 2),
            })

    return pairs[-limit:]


def get_market_change_24h(market: str) -> float | None:
    """24u prijsverandering (%) uit de market_watchlist, of None als onbekend."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT change_24h FROM market_watchlist WHERE market=?", (market,)
        ).fetchone()
    return row["change_24h"] if row else None


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


def reset_paper_trading(starting_capital: float = 1000.0) -> None:
    """Wist alle paper trades, posities en snapshots — reset cash naar startkapitaal."""
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM paper_portfolio")
        conn.execute("INSERT OR REPLACE INTO paper_cash (id, eur) VALUES (1, ?)", (starting_capital,))
        conn.execute("DELETE FROM portfolio_snapshots")
        conn.execute("DELETE FROM daily_pnl")
        conn.execute("DELETE FROM position_meta")


def get_position_meta(market: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT peak_price, breakeven_set, house_money_activated FROM position_meta WHERE market=?",
            (market,)
        ).fetchone()
    return dict(row) if row else {"peak_price": 0.0, "breakeven_set": 0, "house_money_activated": 0}


def update_position_peak(market: str, price: float) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO position_meta (market, peak_price, breakeven_set) VALUES (?,?,0)
            ON CONFLICT(market) DO UPDATE SET peak_price=?
        """, (market, price, price))


def set_breakeven_activated(market: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO position_meta (market, peak_price, breakeven_set) VALUES (?,0,1)
            ON CONFLICT(market) DO UPDATE SET breakeven_set=1
        """, (market,))


def set_house_money_activated(market: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO position_meta (market, peak_price, breakeven_set, house_money_activated)
            VALUES (?,0,0,1)
            ON CONFLICT(market) DO UPDATE SET house_money_activated=1
        """, (market,))


def clear_position_meta(market: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM position_meta WHERE market=?", (market,))


def get_all_positions() -> list[dict]:
    """Geeft alle open paper posities (amount > 0) terug."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT market, amount, avg_price FROM paper_portfolio WHERE amount > 0"
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_trade_pnl(market: str) -> float | None:
    """Geeft de gerealiseerde PnL (EUR) van het meest recente BUY→SELL paar, of None."""
    with get_conn() as conn:
        sell = conn.execute(
            "SELECT price, amount, ts FROM paper_trades WHERE market=? AND side='SELL' ORDER BY ts DESC LIMIT 1",
            (market,)
        ).fetchone()
        if not sell:
            return None
        buy = conn.execute(
            "SELECT price FROM paper_trades WHERE market=? AND side='BUY' AND ts<=? ORDER BY ts DESC LIMIT 1",
            (market, sell["ts"])
        ).fetchone()
        if not buy:
            return None
    return (float(sell["price"]) - float(buy["price"])) * float(sell["amount"])


def get_last_sell_ts(market: str) -> str | None:
    """Geeft de timestamp van het meest recente paper SELL, of None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ts FROM paper_trades WHERE market=? AND side='SELL' ORDER BY ts DESC LIMIT 1",
            (market,)
        ).fetchone()
        return row["ts"] if row else None


def get_last_live_sell_ts(market: str) -> str | None:
    """Geeft de timestamp van het meest recente live SELL (filled), of None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ts FROM live_trades WHERE market=? AND side='SELL' AND status='filled' ORDER BY ts DESC LIMIT 1",
            (market,)
        ).fetchone()
        return row["ts"] if row else None


def save_portfolio_snapshot(cash_eur: float, pos_eur: float, total_eur: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (ts, cash_eur, pos_eur, total_eur) VALUES (?,?,?,?)",
            (_now(), cash_eur, pos_eur, total_eur)
        )


def get_portfolio_snapshots(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def save_backtest_run(
    market: str, interval: str, sma_short: int, sma_long: int,
    rsi_buy: float, rsi_sell: float, capital: float,
    return_pct: float, sharpe: float | None, max_dd: float,
    win_rate: float, num_trades: int,
) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO backtest_runs
              (ts, market, interval, sma_short, sma_long, rsi_buy, rsi_sell,
               capital, return_pct, sharpe, max_dd, win_rate, num_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            _now(),
            market, interval, sma_short, sma_long, rsi_buy, rsi_sell,
            capital, return_pct, sharpe, max_dd, win_rate, num_trades,
        ))


def get_trading_paused() -> bool:
    """Geeft True terug als trading handmatig gepauzeerd is."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key='trading_paused'"
        ).fetchone()
    return row["value"] == "1" if row else False


def set_trading_paused(paused: bool) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('trading_paused', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, ("1" if paused else "0",))


def get_portfolio_peak() -> float:
    """Hoogste portfolio-totaal ooit geregistreerd in snapshots."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(total_eur) AS peak FROM portfolio_snapshots"
        ).fetchone()
    return row["peak"] if row and row["peak"] is not None else 0.0


# --- AI Accuracy ---

def get_pending_accuracy_decisions(horizon_h: float = 8.0) -> list[dict]:
    """Geeft AI-beslissingen terug die ouder zijn dan horizon_h uur en nog niet zijn geëvalueerd."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT d.id, d.market, d.decision, d.confidence, d.entry_price, d.ts
            FROM ai_decisions d
            LEFT JOIN ai_accuracy a ON a.decision_id = d.id
            WHERE a.id IS NULL
              AND d.decision IN ('BUY', 'SELL')
              AND d.entry_price > 0
              AND datetime(d.ts) <= datetime('now', ? || ' hours')
        """, (f"-{horizon_h}",)).fetchall()
    return [dict(r) for r in rows]


def save_ai_accuracy(
    decision_id: int, market: str, decision: str, confidence: float,
    entry_price: float, eval_price: float, pnl_pct: float, outcome: str, horizon_h: float,
) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO ai_accuracy
                (decision_id, market, decision, confidence, entry_price,
                 eval_price, pnl_pct, outcome, horizon_h, eval_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            decision_id, market, decision, confidence, entry_price,
            eval_price, pnl_pct, outcome, horizon_h, _now(),
        ))


def get_ai_accuracy_stats() -> dict:
    """Accuracy-statistieken per markt en overall."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT market, decision,
                   COUNT(*)                                          AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)   AS wins,
                   SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END)  AS losses,
                   AVG(pnl_pct)                                      AS avg_pnl_pct,
                   AVG(confidence)                                   AS avg_confidence
            FROM ai_accuracy
            GROUP BY market, decision
            ORDER BY market, decision
        """).fetchall()
        overall = conn.execute("""
            SELECT COUNT(*)                                          AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)   AS wins,
                   SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END)  AS losses,
                   AVG(pnl_pct)                                      AS avg_pnl_pct
            FROM ai_accuracy
        """).fetchone()
    return {
        "per_market": [dict(r) for r in rows],
        "overall":    dict(overall) if overall else {},
    }


def save_market_advice(market: str, recommended: bool, confidence: float | None, reasoning: str) -> None:
    now = _now()
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


# --- OCO orders ---

def save_oco_order(
    market: str, amount: float,
    tp_order_id: str | None, sl_order_id: str | None,
    tp_price: float | None, sl_price: float | None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO oco_orders (ts, market, amount, tp_order_id, sl_order_id, tp_price, sl_price)
            VALUES (?,?,?,?,?,?,?)
        """, (_now(), market, amount,
              tp_order_id, sl_order_id, tp_price, sl_price))
        return cur.lastrowid


def get_open_oco_orders(market: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM oco_orders WHERE market=? AND status='open' ORDER BY ts DESC",
            (market,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_oco_status(oco_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE oco_orders SET status=? WHERE id=?", (status, oco_id))


def cancel_all_oco_orders(market: str) -> None:
    """Markeer alle open OCO orders als geannuleerd (bijv. na handmatige verkoop)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE oco_orders SET status='cancelled' WHERE market=? AND status='open'",
            (market,)
        )


# --- Live trade PnL helper ---

def save_groq_tokens(tokens: int) -> None:
    """Sla het aantal gebruikte Groq tokens op voor vandaag."""
    today = _today()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO groq_token_log (ts, date, tokens_used) VALUES (?,?,?)",
            (_now(), today, tokens),
        )


def get_groq_daily_tokens() -> int:
    """Geeft het totaal aantal Groq tokens in het rollende 24-uurs venster."""
    from datetime import timedelta
    cutoff = (datetime.now(_AMS) - timedelta(hours=24)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total FROM groq_token_log WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["total"]) if row else 0


def save_google_requests(n: int = 1) -> None:
    """Sla het aantal Google API-verzoeken op (rollend 24u venster)."""
    today = _today()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO google_request_log (ts, date, requests) VALUES (?,?,?)",
            (_now(), today, n),
        )


def get_google_daily_requests() -> int:
    """Geeft het totaal aantal Google API-verzoeken in het rollende 24-uurs venster."""
    from datetime import timedelta
    cutoff = (datetime.now(_AMS) - timedelta(hours=24)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(requests), 0) AS total FROM google_request_log WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["total"]) if row else 0


def save_mistral_tokens(tokens: int) -> None:
    """Sla het aantal gebruikte Mistral tokens op."""
    today = _today()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mistral_token_log (ts, date, tokens_used) VALUES (?,?,?)",
            (_now(), today, tokens),
        )


def get_mistral_daily_tokens() -> int:
    """Geeft het totaal aantal Mistral tokens in het rollende 24-uurs venster."""
    from datetime import timedelta
    cutoff = (datetime.now(_AMS) - timedelta(hours=24)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total FROM mistral_token_log WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["total"]) if row else 0


def save_cerebras_tokens(tokens: int) -> None:
    """Sla het aantal gebruikte Cerebras tokens op."""
    today = _today()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cerebras_token_log (ts, date, tokens_used) VALUES (?,?,?)",
            (_now(), today, tokens),
        )


def get_cerebras_daily_tokens() -> int:
    """Geeft het totaal aantal Cerebras tokens in het rollende 24-uurs venster."""
    from datetime import timedelta
    cutoff = (datetime.now(_AMS) - timedelta(hours=24)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total FROM cerebras_token_log WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["total"]) if row else 0


def get_last_live_trade_pnl(market: str) -> float | None:
    """Gerealiseerde PnL van het meest recente LIVE BUY→SELL paar, of None."""
    with get_conn() as conn:
        sell = conn.execute(
            "SELECT price, amount, ts FROM live_trades "
            "WHERE market=? AND side='SELL' AND status='filled' ORDER BY ts DESC LIMIT 1",
            (market,)
        ).fetchone()
        if not sell:
            return None
        buy = conn.execute(
            "SELECT price FROM live_trades "
            "WHERE market=? AND side='BUY' AND status='filled' AND ts<=? ORDER BY ts DESC LIMIT 1",
            (market, sell["ts"])
        ).fetchone()
        if not buy:
            return None
    return (float(sell["price"]) - float(buy["price"])) * float(sell["amount"])
