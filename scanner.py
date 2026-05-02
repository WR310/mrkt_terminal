import os
import json
from typing import Callable, Awaitable
from core import MRKTClient

DATA_DIR = "data"
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
TOP_N = 50

LogFn = Callable[[str], None]


async def scan_liquidity(
    client: MRKTClient,
    log: LogFn = print,
) -> list[dict]:
    """
    Сканирует /collections, оставляет топ-N по volume,
    сохраняет в data/assets.json и возвращает список.
    """
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
        floor_nano = c.get("floorPriceNanoTons", 0) or 0
        floor_ton = floor_nano / 1_000_000_000
        volume = c.get("volume", 0) or 0
        result.append(
            {
                "name": c.get("name"),
                "title": c.get("title"),
                "floorPriceTon": floor_ton,
                "floorPriceNanoTons": floor_nano,
                "volume": volume,
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
