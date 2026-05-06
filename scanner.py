# scanner.py
import os
import json
import time
import logging
from typing import Callable, Optional, Dict, Tuple

from core import MRKTClient, nano_to_ton

log = logging.getLogger("SCANNER")

DATA_DIR = "data"
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
TOP_N = 50

LogFn = Callable[[str], None]

# ============================================================
#         КЭШ BEST OFFER (Buy Orders) ПО КОЛЛЕКЦИЯМ
# ============================================================
# { collection_id: (best_offer_nano | None, expires_at_ts) }
_BEST_OFFER_CACHE: Dict[str, Tuple[Optional[int], float]] = {}
_DEFAULT_OFFER_TTL = 12.0  # 10–15 сек по ТЗ


def _now() -> float:
    return time.time()


def _extract_order_price_nano(order: dict) -> Optional[int]:
    """
    Достаёт цену ордера в нано-TON. Только явные nano-поля.
    """
    for key in ("priceMaxNanoTONs", "priceNanoTONs", "priceNanoTons", "priceNano"):
        v = order.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


async def get_best_offer(
    client: MRKTClient,
    collection_id: str,
    ttl: float = _DEFAULT_OFFER_TTL,
    log_func: Optional[LogFn] = None,
) -> Optional[int]:
    """
    Возвращает цену лучшего (наибольшего) Buy Order по коллекции — в НАНО-TON.

    Если офферов нет — возвращает None.
    Кэш: повторные вызовы в течение `ttl` секунд возвращают закэшированное значение.

    Использует явный метод client.get_collection_offers — никакого
    "магического" перебора атрибутов клиента.
    """
    if not collection_id:
        return None

    cached = _BEST_OFFER_CACHE.get(collection_id)
    if cached and cached[1] > _now():
        return cached[0]

    try:
        orders = await client.get_collection_offers(collection_id)
    except Exception as e:
        msg = f"[OFFERS] Ошибка запроса офферов {collection_id}: {e}"
        log.warning(msg)
        if log_func:
            log_func(msg)
        # Короткий отрицательный кэш
        _BEST_OFFER_CACHE[collection_id] = (None, _now() + min(ttl, 5.0))
        return None

    if not isinstance(orders, list) or not orders:
        _BEST_OFFER_CACHE[collection_id] = (None, _now() + ttl)
        return None

    best_nano: Optional[int] = None
    for o in orders:
        if not isinstance(o, dict):
            continue
        price = _extract_order_price_nano(o)
        if price is None:
            continue
        if best_nano is None or price > best_nano:
            best_nano = price

    _BEST_OFFER_CACHE[collection_id] = (best_nano, _now() + ttl)
    return best_nano


def invalidate_best_offer(collection_id: Optional[str] = None) -> None:
    if collection_id is None:
        _BEST_OFFER_CACHE.clear()
    else:
        _BEST_OFFER_CACHE.pop(collection_id, None)


# ============================================================
#         СКАНЕР ЛИКВИДНОСТИ
# ============================================================
async def scan_liquidity(
    client: MRKTClient,
    log: LogFn = print,
) -> list[dict]:
    os.makedirs(DATA_DIR, exist_ok=True)

    log("[*] Запрос списка коллекций...")
    collections = await client.get_collections()
    log(f"[+] Получено коллекций: {len(collections)}")

    if not collections:
        log("[!] Список пуст — нечего сохранять.")
        return []

    sorted_cols = sorted(
        collections,
        key=lambda c: c.get("volume", 0) or 0,
        reverse=True,
    )
    top = sorted_cols[:TOP_N]

    result = []
    for c in top:
        floor_nano = int(c.get("floorPriceNanoTons", 0) or 0)
        result.append(
            {
                "name": c.get("name"),
                "title": c.get("title"),
                "floorPriceTon": nano_to_ton(floor_nano),
                "floorPriceNanoTons": floor_nano,
                "volume": c.get("volume", 0) or 0,
            }
        )

    with open(ASSETS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log(f"[+] Сохранено топ-{len(result)} коллекций в {ASSETS_FILE}")
    log("[*] Топ-50 по объёму:")
    for i, c in enumerate(result[:50], 1):
        log(
            f"   {i:>2}. {c['title']:<25} floor={c['floorPriceTon']:.3f} TON | vol={c['volume']:,}"
        )
    return result
