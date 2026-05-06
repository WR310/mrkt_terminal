# sniper.py — боевой модуль "Снайпер" (Tank Edition, Liquidity-Gap aware)
import os
import json
import asyncio
import logging
from typing import Callable, Optional, Awaitable

from core import MRKTClient, nano_to_ton
from database import log_trade, init_db
from telegram_notify import send_telegram_notification

log = logging.getLogger("SNIPER")

DATA_DIR = "data"
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")

# ============================================================
#       ФИКС-СБОР MRKT НА ПРОДАЖУ (TON)
# 0.10 TON — фактический фикс MRKT за листинг/продажу
# 0.05 TON — буфер на корректировку цены
# Итого: 0.15 TON. Никаких 0.4/5% — мы внутри MRKT, без вывода NFT.
# ============================================================
SELL_FIXED_COST_TON = 0.15

LogFn = Callable[[str], None]
CheckRunFn = Callable[[], bool]
GetBestOfferFn = Callable[[str], Awaitable[Optional[int]]]  # collection -> nano|None


def _extract_price_nano(lot: dict) -> int | None:
    for key in ("priceNanoTONs", "priceNanoTons", "priceNano"):
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
    get_best_offer: Optional[GetBestOfferFn] = None,
    check_liquidity_gap: bool = True,
):
    """
    Снайпер с проверкой Liquidity Gap.

    Если check_liquidity_gap=True и get_best_offer задан, то перед покупкой
    смотрим лучший Buy Order по коллекции. Если он >= цены лота —
    лот заберёт владелец оффера, мы пропускаем цель.
    """
    init_db()
    log(f"[*] Снайпер заряжен. Целевая скидка ≥ {target_discount_percent}%.")
    log(
        f"[*] Liquidity Gap check: "
        f"{'ON' if (check_liquidity_gap and get_best_offer) else 'OFF'}"
    )

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
        log(f"[*] Баланс орудия: {current_balance:.3f} TON.")
    except Exception as e:
        log(f"[!] Ошибка проверки баланса: {e}")
        return

    affordable = []
    for asset in assets:
        floor_nano = asset.get("floorPriceNanoTons") or asset.get("floorPriceNano")
        if floor_nano and nano_to_ton(floor_nano) <= current_balance:
            affordable.append(asset)

    top_assets = affordable[:15]
    if not top_assets:
        log("[!] Нет целей по карману. Снайпер отключается.")
        return

    log(f"[*] Целей в прицеле: {len(top_assets)}")
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

            target_buy_nano = int(floor_nano * multiplier)
            target_ton = nano_to_ton(target_buy_nano)
            floor_ton = nano_to_ton(floor_nano)

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
            lot_price_nano = _extract_price_nano(cheapest)
            lot_id = _extract_sale_id(cheapest)
            if not lot_price_nano or not lot_id:
                await asyncio.sleep(delay_between)
                continue

            buy_ton = nano_to_ton(lot_price_nano)

            # 1) Дисконт-фильтр
            if lot_price_nano > target_buy_nano:
                log(
                    f"   [~] {title[:15]:<15} | Флор: {floor_ton:.2f} | "
                    f"Мин: {buy_ton:.2f} | Цель: < {target_ton:.2f}"
                )
                if is_running_flag():
                    await asyncio.sleep(delay_between)
                continue

            # 2) Liquidity Gap — ЖЁСТКИЙ ФИЛЬТР
            if check_liquidity_gap and get_best_offer is not None:
                try:
                    best_offer_nano = await get_best_offer(collection)
                except Exception as e:
                    log(f"   [!] Liquidity Gap check failed ({title}): {e}")
                    best_offer_nano = None

                if best_offer_nano is not None and best_offer_nano >= lot_price_nano:
                    log(
                        f"Snipe blocked by high offer: "
                        f"{nano_to_ton(best_offer_nano):.3f} TON "
                        f"(lot {buy_ton:.3f} TON, {title})"
                    )
                    if is_running_flag():
                        await asyncio.sleep(delay_between)
                    continue

            # 3) Атака
            log(f"\n   [TARGET] {title}: флор {floor_ton:.3f} | лот {buy_ton:.3f}")
            log(f"   [!] АТАКУЮ (Выкуп)...")

            try:
                await client.buy_gift(gift_id=lot_id, price_nano=lot_price_nano)
                log("   [+] Выкуп успешен.")

                sell_price_nano = floor_nano - undercut_nano
                sell_ton = nano_to_ton(sell_price_nano)
                log(f"   [*] Выставляю на продажу за {sell_ton:.4f} TON...")

                await client.sell_gifts(
                    gift_ids=[lot_id], prices_nano=[sell_price_nano]
                )
                log(f"   [✓] ФЛИП ЗАВЕРШЁН: {title} снова на витрине.\n")

                # Чистая прибыль = sell_ton - buy_ton - SELL_FIXED_COST_TON
                profit_ton = sell_ton - buy_ton - SELL_FIXED_COST_TON

                log_trade(
                    trade_type="flip",
                    collection=title,
                    price_ton=buy_ton,
                    profit_ton=profit_ton,
                    backdrop=cheapest.get("backdropName"),
                    model=cheapest.get("modelName"),
                    extra=f"sell_ton={sell_ton:.4f};fee_ton={SELL_FIXED_COST_TON:.2f}",
                )

                # Plain-text алерт. Без HTML, без блоков, без декораций.
                msg = (
                    f"Куплено: {title}\n"
                    f"Цена покупки: {buy_ton:.3f} TON\n"
                    f"Цена продажи: {sell_ton:.3f} TON\n"
                    f"Фикс. сбор: {SELL_FIXED_COST_TON:.2f} TON\n"
                    f"Чистый профит: {profit_ton:+.3f} TON"
                )
                asyncio.create_task(send_telegram_notification(msg))

            except Exception as e:
                log(f"   [X] Ошибка при атаке/флипе ({title}): {e}\n")

            if is_running_flag():
                await asyncio.sleep(delay_between)

    log("\n[i] Снайпер возвращён на базу. Патрулирование остановлено.")
