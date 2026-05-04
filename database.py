import os
import sqlite3
import threading
from datetime import datetime, timezone

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "trades.db")

_LOCK = threading.Lock()
_initialized = False


def _ensure_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    """Создаёт таблицу trades, если её нет. Идемпотентно."""
    global _initialized
    with _LOCK:
        if _initialized:
            return
        conn = _get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    type        TEXT    NOT NULL,        -- 'buy' | 'sell' | 'flip' | 'bid'
                    collection  TEXT,
                    model       TEXT,
                    backdrop    TEXT,
                    price_ton   REAL,
                    profit_ton  REAL,
                    extra       TEXT,
                    timestamp   TEXT    NOT NULL
                );
                """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_type ON trades(type);")
            conn.commit()
            _initialized = True
        finally:
            conn.close()


def log_trade(
    trade_type: str,
    collection: str | None = None,
    price_ton: float | None = None,
    profit_ton: float | None = None,
    model: str | None = None,
    backdrop: str | None = None,
    extra: str | None = None,
) -> None:
    """
    Записывает сделку в журнал. Безопасно вызывать из любого потока.
    Все ошибки глушатся в print — БД никогда не должна валить торговый цикл.
    """
    if not _initialized:
        init_db()

    ts = datetime.now(timezone.utc).isoformat()

    try:
        with _LOCK:
            conn = _get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO trades
                        (type, collection, model, backdrop, price_ton, profit_ton, extra, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_type,
                        collection,
                        model,
                        backdrop,
                        float(price_ton) if price_ton is not None else None,
                        float(profit_ton) if profit_ton is not None else None,
                        extra,
                        ts,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"[DB] Ошибка записи trade: {e}")


def fetch_recent(limit: int = 50) -> list[dict]:
    """Полезно для будущей вкладки 'История сделок' в GUI."""
    if not _initialized:
        init_db()
    try:
        with _LOCK:
            conn = _get_conn()
            try:
                cur = conn.execute(
                    """
                    SELECT id, type, collection, model, backdrop,
                           price_ton, profit_ton, extra, timestamp
                    FROM trades
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            finally:
                conn.close()
    except Exception as e:
        print(f"[DB] Ошибка чтения trades: {e}")
        return []
