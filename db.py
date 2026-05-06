# db.py
import asyncio
import logging
import time
from typing import Optional, Dict, Any

import aiosqlite

log = logging.getLogger("DB")

DB_PATH = "trades.db"

# ============================================================
# ФИКСИРОВАННАЯ ИЗДЕРЖКА НА ПРОДАЖУ (TON)
# 0.10 (фикс MRKT) + 0.05 (буфер на корректировку цены) = 0.15
# Никаких 5% и 0.4 TON — внутри MRKT нет ни процентной комиссии,
# ни сбора за вывод NFT (мы продаём там же).
# ============================================================
SELL_FIXED_COST_TON = 0.15


class TradeJournal:
    """
    Асинхронный журнал сделок на SQLite (aiosqlite).
    PnL считается с учётом SELL_FIXED_COST_TON на каждую продажу.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def init(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        action     TEXT    NOT NULL CHECK(action IN ('buy','sell')),
                        price_ton  REAL    NOT NULL,
                        timestamp  INTEGER NOT NULL,
                        meta       TEXT
                    )
                """)
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_action ON trades(action)"
                )
                await db.commit()
            self._initialized = True
            log.info("Trade journal initialized at %s", self.db_path)

    async def log_trade(
        self,
        action: str,
        price_ton: float,
        meta: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> int:
        if action not in ("buy", "sell"):
            raise ValueError(f"Bad action: {action!r}")
        try:
            price = float(price_ton)
        except (TypeError, ValueError):
            raise ValueError(f"Bad price: {price_ton!r}")

        ts = int(timestamp if timestamp is not None else time.time())

        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO trades(action, price_ton, timestamp, meta) "
                "VALUES (?, ?, ?, ?)",
                (action, price, ts, meta),
            )
            await db.commit()
            row_id = cur.lastrowid

        log.info("trade logged: #%s %s %.4f TON (meta=%s)", row_id, action, price, meta)
        return row_id

    async def _stats_for_window(
        self, db: aiosqlite.Connection, since_ts: Optional[int]
    ) -> Dict[str, Any]:
        if since_ts is not None:
            ts_clause = "timestamp >= ? AND"
            params: tuple = (since_ts,)
        else:
            ts_clause = ""
            params = ()

        async with db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(price_ton), 0) "
            f"FROM trades WHERE {ts_clause} action='buy'",
            params,
        ) as cur:
            buys_count, buys_sum = await cur.fetchone()

        async with db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(price_ton), 0) "
            f"FROM trades WHERE {ts_clause} action='sell'",
            params,
        ) as cur:
            sells_count, sells_sum = await cur.fetchone()

        buys_count = int(buys_count or 0)
        sells_count = int(sells_count or 0)
        buys_sum = float(buys_sum or 0.0)
        sells_gross = float(sells_sum or 0.0)

        fixed_costs = sells_count * SELL_FIXED_COST_TON
        sells_net = sells_gross - fixed_costs

        pnl_gross = sells_gross - buys_sum
        pnl_net = sells_net - buys_sum
        roi = (pnl_net / buys_sum * 100.0) if buys_sum > 0 else 0.0

        return {
            "buys_count": buys_count,
            "sells_count": sells_count,
            "invested_ton": buys_sum,
            "received_gross_ton": sells_gross,
            "received_net_ton": sells_net,
            "pnl_gross_ton": pnl_gross,
            "pnl_net_ton": pnl_net,
            "roi_percent": roi,
            "fixed_cost_per_sell_ton": SELL_FIXED_COST_TON,
            "fixed_costs_total_ton": fixed_costs,
        }

    async def get_pnl_stats(self) -> Dict[str, Dict[str, Any]]:
        await self.init()
        now = int(time.time())
        windows = {
            "24h": now - 24 * 3600,
            "7d": now - 7 * 24 * 3600,
            "all": None,
        }
        result: Dict[str, Dict[str, Any]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            for key, since in windows.items():
                result[key] = await self._stats_for_window(db, since)
        return result

    async def recent_trades(self, limit: int = 20) -> list[dict]:
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, action, price_ton, timestamp, meta "
                "FROM trades ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


journal = TradeJournal()
