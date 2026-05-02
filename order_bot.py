import os
import json
import asyncio
from typing import Callable

from core import MRKTClient

DATA_DIR = "data"
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
OFFERS_LOG = os.path.join(DATA_DIR, "offers_log.json")

LogFn = Callable[[str], None]


def _extract_price_nano(lot: dict) -> int | None:
    """Достаёт цену лота в наноТОНах из разных возможных полей."""
    for key in ("priceNanoTons", "priceNano", "price", "salePrice"):
        v = lot.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def _extract_sale_id(lot: dict) -> str | None:
    """Достаёт giftSaleId лота из разных возможных полей."""
    for key in ("giftSaleId", "saleId", "id", "_id"):
        v = lot.get(key)
        if isinstance(v, str) and v:
            return v
    return None


import os
import json
import asyncio


async def clear_all_offers(client, log_func):
    log_func("[*] Запуск ковровой зачистки Глобальных Ордеров...")
    assets_file = os.path.join("data", "assets.json")

    if not os.path.exists(assets_file):
        log_func("[!] Нет базы assets.json. Нечего чистить.")
        return

    with open(assets_file, "r", encoding="utf-8") as f:
        assets = json.load(f)

    deleted_count = 0

    for asset in assets:
        collection = asset.get("name")
        if not collection:
            continue

        try:
            # Запрашиваем стакан коллекции
            orders = await client.get_collection_orders(collection, count=50)

            # Ищем только свои ставки через наш любимый флаг isMine
            my_orders = [o for o in orders if o.get("isMine")]

            for order in my_orders:
                order_id = order.get("id")
                if order_id:
                    await client.cancel_collection_order(order_id)
                    log_func(f"   [-] Снята ставка в коллекции: {collection[:15]}")
                    deleted_count += 1
                    await asyncio.sleep(0.5)

        except Exception as e:
            log_func(f"   [X] Ошибка проверки {collection[:10]}: {e}")

        # Пауза между коллекциями, чтобы сервер не забанил за спам
        await asyncio.sleep(1)

    log_func(
        f"[✓] Чистка завершена. Удалено ордеров: {deleted_count} шт. Баланс разблокирован."
    )


async def run_mass_offers(
    client: MRKTClient,
    discount_percent: float,
    log: LogFn = print,
    delay_between: float = 1.5,
) -> list[dict]:
    """
    Массовая постановка офферов:
      1. Читает data/assets.json.
      2. Проверяет баланс пользователя.
      3. Ставит офферы только на те лоты, на которые хватает денег.
    """
    if not os.path.exists(ASSETS_FILE):
        log(f"[!] Файл {ASSETS_FILE} не найден. Сначала запустите сканер.")
        return []

    with open(ASSETS_FILE, "r", encoding="utf-8") as f:
        assets = json.load(f)

    if not assets:
        log("[!] assets.json пуст.")
        return []

    if discount_percent <= 0 or discount_percent >= 100:
        log(f"[!] Некорректный процент скидки: {discount_percent}")
        return []

    try:
        current_balance = await client.get_balance()
    except Exception as e:
        log(f"[!] Ошибка проверки баланса: {e}")
        return []

    multiplier = 1.0 - (discount_percent / 100.0)

    # --- ПРЕДВАРИТЕЛЬНЫЙ ФИЛЬТР БЮДЖЕТА ---
    affordable_assets = []
    for asset in assets:
        floor_nano = asset.get("floorPriceNanoTons") or asset.get("floorPriceNano")
        if not floor_nano:
            continue

        expected_offer_ton = (floor_nano * multiplier) / 1_000_000_000
        if expected_offer_ton <= current_balance:
            affordable_assets.append(asset)

    top_assets = affordable_assets[:30]

    log(f"[*] Доступный баланс орудия: {current_balance:.3f} TON")
    log(
        f"[*] Массовые офферы — дисконт {discount_percent:.1f}% (множитель {multiplier:.4f})"
    )
    log(f"[*] Отсеяно недоступных: {len(assets) - len(affordable_assets)} шт.")
    log(f"[*] Коллекций по карману к обработке: {len(top_assets)}")

    results: list[dict] = []

    for i, asset in enumerate(top_assets, 1):
        name = asset.get("name")
        title = asset.get("title") or name
        if not name:
            continue

        try:
            lots = await client.get_listings(
                collection_name=name,
                count=5,
                cursor="",
                ordering="Price",
                low_to_high=True,
            )
        except Exception as e:
            log(f"   [{i:>2}] {title:<15} [!] ошибка радара: {e}")
            results.append({"collection": name, "status": "list_error"})
            continue

        if not lots:
            log(f"   [{i:>2}] {title:<15} — лотов не найдено")
            results.append({"collection": name, "status": "no_lots"})
            continue

        cheapest = lots[0]
        price_nano = _extract_price_nano(cheapest)
        sale_id = _extract_sale_id(cheapest)

        if price_nano is None or sale_id is None:
            log(f"   [{i:>2}] {title:<15} [!] не удалось распарсить лот")
            results.append({"collection": name, "status": "parse_error"})
            continue

        offer_nano = int(price_nano * multiplier)
        price_ton = price_nano / 1_000_000_000
        offer_ton = offer_nano / 1_000_000_000

        if offer_ton > current_balance:
            log(f"   [{i:>2}] {title:<15} [skip] {offer_ton:.2f} TON > баланса")
            results.append({"collection": name, "status": "insufficient_funds"})
            continue

        try:
            resp = await client.create_offer(sale_id, offer_nano)
            log(
                f"   [{i:>2}] {title[:15]:<15} floor={price_ton:.3f} → offer={offer_ton:.3f} TON  [OK]"
            )
            results.append(
                {
                    "collection": name,
                    "title": title,
                    "saleId": sale_id,
                    "floorTon": price_ton,
                    "offerTon": offer_ton,
                    "status": "ok",
                    "response": resp,
                }
            )

            # Уменьшаем виртуальный баланс
            current_balance -= offer_ton

        except Exception as e:
            log(f"   [{i:>2}] {title[:15]:<15} [!] Ошибка API: {e}")
            results.append(
                {"collection": name, "status": "offer_error", "error": str(e)}
            )

        if delay_between > 0:
            await asyncio.sleep(delay_between)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OFFERS_LOG, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    ok = sum(1 for r in results if r.get("status") == "ok")
    log(f"\n[+] Готово. Успешных офферов: {ok}/{len(results)}")
    log(f"[+] Лог сохранён в {OFFERS_LOG}")

    return results
