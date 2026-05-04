# controller.py
import asyncio
import logging
from typing import Callable, Optional

from core import MRKTClient
from scanner import scan_liquidity
from order_bot import run_mass_offers, clear_all_offers
from flipper import run_auto_flip
from sniper import run_sniper
from engine import run_engine
from watcher import run_watcher
from config_manager import load_config, update_value

log = logging.getLogger("CONTROLLER")


class TerminalController:
    """
    Единый фасад над всеми торговыми модулями.
    Используется и из GUI, и из Telegram-бота, и из CLI.
    Гарантирует, что Движок/Снайпер запущены максимум в одном экземпляре.
    """

    def __init__(self, client: MRKTClient, loop: asyncio.AbstractEventLoop):
        self.client = client
        self.loop = loop

        # Флаги состояния
        self.is_engine_running: bool = False
        self.is_sniper_running: bool = False

        # Фоновые задачи
        self._engine_task: Optional[asyncio.Task] = None
        self._sniper_task: Optional[asyncio.Task] = None
        self._watcher_task: Optional[asyncio.Task] = None

        # Внешние логгеры (GUI подменяет на свои Textbox-логгеры)
        self.engine_logger: Callable[[str], None] = lambda s: log.info("[ENGINE] %s", s)
        self.sniper_logger: Callable[[str], None] = lambda s: log.info("[SNIPER] %s", s)
        self.offers_logger: Callable[[str], None] = lambda s: log.info("[OFFERS] %s", s)
        self.scan_logger: Callable[[str], None] = lambda s: log.info("[SCAN] %s", s)

    # ---------- ENGINE ----------
    async def engine_start(self) -> str:
        if self.is_engine_running:
            return "⚠️ Движок уже работает."

        cfg = load_config()
        discount = float(cfg.get("engine_discount", 15.0))
        bgs = cfg.get("engine_backdrops", {}) or {}

        if bgs.get("Любой"):
            selected = []
        else:
            selected = [k for k, v in bgs.items() if k != "Любой" and v]

        self.is_engine_running = True

        async def _runner():
            try:
                self._watcher_task = asyncio.create_task(
                    run_watcher(
                        self.client, is_running_flag=lambda: self.is_engine_running
                    )
                )
                await run_engine(
                    self.client,
                    discount_percent=discount,
                    log=self.engine_logger,
                    is_running_flag=lambda: self.is_engine_running,
                    target_backdrops=selected,
                )
            except Exception as e:
                log.exception("Engine crashed")
                self.engine_logger(f"[!] Движок упал: {e}")
            finally:
                self.is_engine_running = False

        self._engine_task = asyncio.create_task(_runner())
        return (
            f"✅ Движок запущен. Скидка {discount:.0f}% | фоны: {selected or 'любые'}"
        )

    async def engine_stop(self) -> str:
        if not self.is_engine_running:
            return "⚠️ Движок и так выключен."
        self.is_engine_running = False
        return "🛑 Сигнал на остановку Движка отправлен."

    # ---------- SNIPER ----------
    async def sniper_start(self) -> str:
        if self.is_sniper_running:
            return "⚠️ Снайпер уже в засаде."

        discount = float(load_config().get("sniper_discount", 20.0))
        self.is_sniper_running = True

        async def _runner():
            try:
                await run_sniper(
                    self.client,
                    target_discount_percent=discount,
                    log=self.sniper_logger,
                    is_running_flag=lambda: self.is_sniper_running,
                )
            except Exception as e:
                log.exception("Sniper crashed")
                self.sniper_logger(f"[!] Снайпер упал: {e}")
            finally:
                self.is_sniper_running = False

        self._sniper_task = asyncio.create_task(_runner())
        return f"🎯 Снайпер активирован. Цель: дисконт ≥ {discount:.0f}%"

    async def sniper_stop(self) -> str:
        if not self.is_sniper_running:
            return "⚠️ Снайпер и так выключен."
        self.is_sniper_running = False
        return "🛑 Снайпер: сигнал на отбой отправлен."

    # ---------- ONE-SHOTS ----------
    async def run_scanner(
        self, log_func: Optional[Callable[[str], None]] = None
    ) -> str:
        logger = log_func or self.scan_logger
        try:
            await scan_liquidity(self.client, log=logger)
            return "✅ Сканирование ликвидности завершено."
        except Exception as e:
            return f"❌ Ошибка сканера: {e}"

    async def run_mass_offers(
        self, log_func: Optional[Callable[[str], None]] = None
    ) -> str:
        discount = float(load_config().get("offers_discount", 15.0))
        logger = log_func or self.offers_logger
        try:
            await run_mass_offers(self.client, discount_percent=discount, log=logger)
            return f"✅ Массовые офферы выставлены (дисконт {discount:.0f}%)."
        except Exception as e:
            return f"❌ Ошибка офферов: {e}"

    async def run_flip(self, log_func: Optional[Callable[[str], None]] = None) -> str:
        logger = log_func or self.offers_logger
        try:
            await run_auto_flip(self.client, log_func=logger)
            return "✅ Авто-флип инвентаря завершён."
        except Exception as e:
            return f"❌ Ошибка флипа: {e}"

    async def clear_offers(
        self, log_func: Optional[Callable[[str], None]] = None
    ) -> str:
        logger = log_func or self.offers_logger
        try:
            await clear_all_offers(self.client, log_func=logger)
            return "✅ Все офферы отменены."
        except Exception as e:
            return f"❌ Ошибка отмены офферов: {e}"

    # ---------- STATUS ----------
    async def status(self) -> dict:
        try:
            balance = await self.client.get_balance()
        except Exception as e:
            balance = None
            log.warning("balance fetch failed: %s", e)

        try:
            gifts = await self.client.get_inventory()
            total_nano = sum(
                g.get("floorPriceNanoTONsByCollection", 0) or 0 for g in gifts
            )
            portfolio_ton = total_nano / 10**9
            items = len(gifts)
        except Exception as e:
            log.warning("inventory fetch failed: %s", e)
            portfolio_ton, items = None, None

        cfg = load_config()
        return {
            "balance": balance,
            "portfolio_ton": portfolio_ton,
            "items": items,
            "engine": self.is_engine_running,
            "sniper": self.is_sniper_running,
            "engine_discount": cfg.get("engine_discount"),
            "offers_discount": cfg.get("offers_discount"),
            "sniper_discount": cfg.get("sniper_discount"),
        }

    # ---------- SETTINGS ----------
    def set_discount(self, target: str, value: float) -> str:
        target = target.lower().strip()
        mapping = {
            "engine": "engine_discount",
            "offers": "offers_discount",
            "sniper": "sniper_discount",
            "calc": "calc_discount",
        }
        if target not in mapping:
            return f"❌ Неизвестная цель «{target}». Допустимо: {', '.join(mapping)}."
        if not (0 < value <= 95):
            return "❌ Скидка должна быть в диапазоне 1..95."
        update_value(mapping[target], float(value))
        return f"✅ {target}_discount = {value:.1f}%"
