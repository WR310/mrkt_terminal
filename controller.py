# controller.py
import asyncio
import logging
from typing import Callable, Optional, Any

from core import MRKTClient, nano_to_ton
from scanner import scan_liquidity, get_best_offer, invalidate_best_offer
from order_bot import run_mass_offers, clear_all_offers
from flipper import run_auto_flip
from sniper import run_sniper
from engine import run_engine
from watcher import run_watcher
from config_manager import load_config, update_value
from db import journal

log = logging.getLogger("CONTROLLER")


class _JournalingClient:
    """
    Прозрачный прокси над MRKTClient. Делегирует все методы реальному клиенту,
    но buy/sell-операции дополнительно журналит.
    """

    BUY_METHODS = {
        "buy",
        "buy_gift",
        "buy_listing",
        "purchase_gift",
        "snipe_buy",
        "place_buy",
    }
    SELL_METHODS = {
        "sell_gifts",
        "sell_gift",
        "list_gift",
        "list_for_sale",
        "place_listing",
    }

    def __init__(self, real: MRKTClient):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr
        if name in self.BUY_METHODS:
            return self._wrap(attr, action="buy")
        if name in self.SELL_METHODS:
            return self._wrap(attr, action="sell")
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._real, name, value)

    def _wrap(self, fn: Callable, action: str) -> Callable:
        if not asyncio.iscoroutinefunction(fn):
            # Все торговые методы MRKT — async. Sync-ветка нам не нужна.
            return fn

        async def _async_wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            try:
                await self._journal_from_call(action, args, kwargs, result)
            except Exception as e:
                log.warning("journal write failed (%s): %s", action, e)
            return result

        return _async_wrapper

    @staticmethod
    def _extract_price_ton(args: tuple, kwargs: dict, result: Any) -> Optional[float]:
        # Явные TON-поля
        for key in ("price_ton", "amount_ton", "total_ton"):
            if kwargs.get(key) is not None:
                try:
                    v = float(kwargs[key])
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass

        # Явные nano-поля
        for key in ("price_nano", "priceNano", "price"):
            if kwargs.get(key) is not None:
                v = nano_to_ton(kwargs[key])
                if v > 0:
                    return v

        if isinstance(result, dict):
            for key in ("priceNano", "priceNanoTONs", "totalPriceNano"):
                if result.get(key) is not None:
                    v = nano_to_ton(result[key])
                    if v > 0:
                        return v
        return None

    async def _journal_from_call(
        self, action: str, args: tuple, kwargs: dict, result: Any
    ) -> None:
        if isinstance(result, dict):
            if result.get("ok") is False or result.get("success") is False:
                return
            if result.get("error"):
                return

        price = self._extract_price_ton(args, kwargs, result)
        if price is None or price <= 0:
            log.debug("skip journal: cannot derive price for %s", action)
            return

        meta = None
        if isinstance(result, dict):
            gid = result.get("giftId") or result.get("id") or result.get("listingId")
            if gid:
                meta = f"id={gid}"

        await journal.log_trade(action=action, price_ton=price, meta=meta)


class TerminalController:
    """Единый фасад над всеми торговыми модулями."""

    def __init__(self, client: MRKTClient, loop: asyncio.AbstractEventLoop):
        self.client = _JournalingClient(client)
        self._raw_client = client
        self.loop = loop

        self.is_engine_running: bool = False
        self.is_sniper_running: bool = False

        self._engine_task: Optional[asyncio.Task] = None
        self._sniper_task: Optional[asyncio.Task] = None
        self._watcher_task: Optional[asyncio.Task] = None

        self.engine_logger: Callable[[str], None] = lambda s: log.info("[ENGINE] %s", s)
        self.sniper_logger: Callable[[str], None] = lambda s: log.info("[SNIPER] %s", s)
        self.offers_logger: Callable[[str], None] = lambda s: log.info("[OFFERS] %s", s)
        self.scan_logger: Callable[[str], None] = lambda s: log.info("[SCAN] %s", s)

        try:
            asyncio.get_event_loop().create_task(journal.init())
        except RuntimeError:
            pass

    # ---------- ENGINE ----------
    async def engine_start(self) -> str:
        if self.is_engine_running:
            return "⚠️ Движок уже работает."

        await journal.init()

        cfg = load_config()
        discount = float(cfg.get("engine_discount", 15.0))
        bgs = cfg.get("engine_backdrops", {}) or {}

        if bgs.get("Любой"):
            selected: list[str] = []
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
    def _build_gap_checker(self, ttl: float):
        async def _checker(collection_id: str):
            return await get_best_offer(
                self.client,
                collection_id,
                ttl=ttl,
                log_func=self.sniper_logger,
            )

        return _checker

    async def sniper_start(self) -> str:
        if self.is_sniper_running:
            return "⚠️ Снайпер уже в засаде."

        await journal.init()

        cfg = load_config()
        discount = float(cfg.get("sniper_discount", 20.0))
        check_offers = bool(cfg.get("sniper_check_offers", True))
        offer_ttl = float(cfg.get("sniper_offer_ttl", 12.0))

        invalidate_best_offer()
        gap_checker = self._build_gap_checker(offer_ttl) if check_offers else None

        self.is_sniper_running = True

        async def _runner():
            try:
                await run_sniper(
                    self.client,
                    target_discount_percent=discount,
                    log=self.sniper_logger,
                    is_running_flag=lambda: self.is_sniper_running,
                    get_best_offer=gap_checker,
                    check_liquidity_gap=check_offers,
                )
            except Exception as e:
                log.exception("Sniper crashed")
                self.sniper_logger(f"[!] Снайпер упал: {e}")
            finally:
                self.is_sniper_running = False

        self._sniper_task = asyncio.create_task(_runner())

        gap_str = (
            f" | gap-check: ON (ttl={offer_ttl:.0f}s)"
            if check_offers
            else " | gap-check: OFF"
        )
        return f"🎯 Снайпер активирован. Цель: дисконт ≥ {discount:.0f}%{gap_str}"

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
            portfolio_ton = nano_to_ton(total_nano)
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
            "sniper_check_offers": cfg.get("sniper_check_offers"),
            "sniper_offer_ttl": cfg.get("sniper_offer_ttl"),
        }

    # ---------- PNL ----------
    async def get_pnl(self) -> dict:
        return await journal.get_pnl_stats()

    async def log_manual_trade(
        self, action: str, price_ton: float, meta: Optional[str] = None
    ) -> int:
        return await journal.log_trade(action=action, price_ton=price_ton, meta=meta)

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

    def set_sniper_gap_check(self, enabled: bool, ttl: Optional[float] = None) -> str:
        update_value("sniper_check_offers", bool(enabled))
        msg = f"✅ sniper_check_offers = {bool(enabled)}"
        if ttl is not None:
            if not (1.0 <= float(ttl) <= 120.0):
                return "❌ TTL должен быть в диапазоне 1..120 секунд."
            update_value("sniper_offer_ttl", float(ttl))
            msg += f", sniper_offer_ttl = {float(ttl):.1f}s"
        invalidate_best_offer()
        return msg
