"""
sniper.py — боевой модуль "Снайпер" с Telegram-отчетами (Tank Edition)
"""

import os
import json
import asyncio
import aiohttp
from typing import Callable

from core import MRKTClient
from database import log_trade, init_db

DATA_DIR = "data"
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")

LogFn = Callable[[str], None]
CheckRunFn = Callable[[], bool]


# --- ТЕЛЕГРАМ АЛЕРТЫ ---
async def send_tg_alert(message: str):
    """Легкий асинхронный пуш в Telegram."""
    bot_token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")

    if not bot_token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json=payload)
    except Exception:
        pass


def _extract_price_nano(lot: dict) -> int | None:
    for key in ("priceNanoTONs", "priceNanoTons", "priceNano", "price", "salePrice"):
        v = lot.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def _extract_sale_id(lot: dict) -> str | None:
    for key in ("id", "giftSaleId", "saleId", "_id"):
        v = lot.get(key)
        if isinstance(v, str) and v:
            return v
    return None


async def run_sniper(
    client: MRKTClient,
    target_discount_percent: float,
    log: LogFn,
    is_running_flag: CheckRunFn,
    delay_between: float = 1.0,
    undercut_nano: int = 1,
):
    init_db()
    log(f"[*] Снайпер заряжен. Ищем цели со скидкой от {target_discount_percent}%...")

    if not os.path.exists(ASSETS_FILE):
        log(f"[!] Файл {ASSETS_FILE} не найден. Сначала запустите Сканер!")
        return

    try:
        with open(ASSETS_FILE, "r", encoding="utf-8") as f:
            assets = json.load(f)
    except Exception as e:
        log(f"[!] Ошибка чтения базы: {e}")
        return

    if not assets:
        log("[!] База активов пуста.")
        return

    try:
        current_balance = await client.get_balance()
        log(
            f"[*] Баланс орудия: {current_balance:.3f} TON. Отсекаем недоступные цели..."
        )
    except Exception as e:
        log(f"[!] Ошибка проверки баланса: {e}")
        return

    affordable_assets = []
    for asset in assets:
        floor_nano = asset.get("floorPriceNanoTons") or asset.get("floorPriceNano")
        if floor_nano and (floor_nano / 1_000_000_000) <= current_balance:
            affordable_assets.append(asset)

    top_assets = affordable_assets[:15]

    if not top_assets:
        log("[!] Нет целей по карману. Снайпер отключается.")
        return

    log(f"[*] Выхожу на охоту. Целей в прицеле: {len(top_assets)}")
    log("[i] Для остановки нажмите красную кнопку 'ОСТАНОВИТЬ'.")

    multiplier = 1.0 - (target_discount_percent / 100.0)

    while is_running_flag():
        for asset in top_assets:
            if not is_running_flag():
                break

            collection = asset.get("name")
            title = asset.get("title") or collection
            floor_nano = asset.get("floorPriceNanoTons") or asset.get("floorPriceNano")

            if not collection or not floor_nano:
                continue

            target_buy_price_nano = int(floor_nano * multiplier)
            target_ton = target_buy_price_nano / 1_000_000_000
            floor_ton = floor_nano / 1_000_000_000

            try:
                lots = await client.get_listings(
                    collection_name=collection,
                    count=5,
                    ordering="Price",
                    low_to_high=True,
                )
            except Exception as e:
                log(f"   [!] Ошибка радара ({title}): {e}")
                await asyncio.sleep(delay_between)
                continue

            if not lots:
                await asyncio.sleep(delay_between)
                continue

            cheapest = lots[0]
            lot_price = _extract_price_nano(cheapest)
            lot_id = _extract_sale_id(cheapest)

            if not lot_price or not lot_id:
                await asyncio.sleep(delay_between)
                continue

            buy_ton = lot_price / 1_000_000_000

            if lot_price <= target_buy_price_nano:
                log(f"\n   [🎯] ЦЕЛЬ ОБНАРУЖЕНА: {title}")
                log(f"   [>] Флор: {floor_ton:.3f} TON | Нашли за: {buy_ton:.3f} TON")
                log(f"   [!] АТАКУЮ (Выкуп)...")

                try:
                    await client.buy_gift(gift_id=lot_id, price_nano=lot_price)
                    log("   [+] Выкуп успешен!")

                    sell_price_nano = floor_nano - undercut_nano
                    sell_ton = sell_price_nano / 1_000_000_000
                    log(f"   [*] Выставляю на продажу за {sell_ton:.4f} TON...")

                    await client.sell_gifts(
                        gift_ids=[lot_id], prices_nano=[sell_price_nano]
                    )
                    log(f"   [✓] ФЛИП ЗАВЕРШЕН: {title} снова на витрине.\n")

                    profit_ton = sell_ton - buy_ton

                    # === Запись в Trade Journal ===
                    log_trade(
                        trade_type="flip",
                        collection=title,
                        price_ton=buy_ton,
                        profit_ton=profit_ton,
                        backdrop=cheapest.get("backdropName"),
                        model=cheapest.get("modelName"),
                        extra=f"sell_ton={sell_ton:.4f}",
                    )

                    alert_msg = (
                        f"🎯 <b>ЦЕЛЬ ПОРАЖЕНА: {title}</b>\n\n"
                        f"🛒 <b>Куплен за:</b> {buy_ton:.3f} TON\n"
                        f"💰 <b>На витрине за:</b> {sell_ton:.3f} TON\n"
                        f"💵 <b>Чистая прибыль:</b> +{profit_ton:.3f} TON\n\n"
                        f"⚙️ <i>MRKT Terminal PRO | Tank Edition</i>"
                    )
                    asyncio.create_task(send_tg_alert(alert_msg))

                except Exception as e:
                    log(f"   [X] Ошибка при атаке/флипе ({title}): {e}\n")
            else:
                log(
                    f"   [~] {title[:15]:<15} | Флор: {floor_ton:.2f} | Мин. на рынке: {buy_ton:.2f} | Ждем: < {target_ton:.2f}"
                )

            if is_running_flag():
                await asyncio.sleep(delay_between)

    log("\n[i] Снайпер возвращен на базу. Патрулирование остановлено.")
