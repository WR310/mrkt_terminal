import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Файл будет сохраняться в папку data
ASSETS_FILE = Path("data/assets.json")


async def scan_liquidity(client):
    log.info("Запускаем сканер ликвидности...")

    # 1. Запрашиваем все коллекции
    try:
        response = await client.get_collections()
    except Exception as e:
        log.error(f"Ошибка при запросе коллекций: {e}")
        return

    # Защита от неожиданной структуры ответа (словарь или список)
    collections = (
        response.get("items", response) if isinstance(response, dict) else response
    )

    if not collections or not isinstance(collections, list):
        log.error("API вернуло неожиданный формат. Смотрим сырой ответ:")
        print(response)
        return

    log.info(f"Получено {len(collections)} коллекций от МРКТ.")

    # Выводим структуру первой коллекции для дебага
    print("\n--- СТРУКТУРА ПЕРВОЙ КОЛЛЕКЦИИ ДЛЯ ДЕБАГА ---")
    print(json.dumps(collections[0], indent=2, ensure_ascii=False))
    print("---------------------------------------------\n")

    liquid_assets = []

    # 2. Фильтруем коллекции
    for item in collections:
        # Временная заглушка ключей. После дебага мы заменим "slug" и "sales" на реальные ключи МРКТ
        slug = item.get("slug") or item.get("id") or item.get("name")

        # Пытаемся вытащить данные о продажах/обороте.
        # Если API отдает статистику отдельно, нам придется делать доп. запросы.
        volume = item.get("volume", 0)
        sales = item.get("sales", 0)

        # Пока фильтруем мягко, просто чтобы создать файл
        if slug:
            liquid_assets.append(
                {
                    "slug": slug,
                    "name": item.get("name", "Unknown"),
                    "floor": item.get("floorPrice", 0),
                    "volume": volume,
                }
            )

    # 3. Сохраняем в файл
    ASSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ASSETS_FILE, "w", encoding="utf-8") as f:
        json.dump(liquid_assets, f, indent=4, ensure_ascii=False)

    log.info(
        f"Сканирование завершено. В assets.json сохранено {len(liquid_assets)} ликвидных подарков."
    )
    return liquid_assets
