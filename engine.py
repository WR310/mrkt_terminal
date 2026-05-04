import os
import json
import asyncio

from telegram_notify import send_telegram_notification
from database import log_trade, init_db

# ============================================================
# Шаги ставок в нано-тонах
# ============================================================
STEP_FIRST_BID_NANO = 10_000_000  # 0.01 TON  — первая ставка в коллекции
STEP_OVERBID_NANO = 2_000_000  # 0.002 TON — перебивка чужой ставки

# ============================================================
# Ресинк баланса
# ============================================================
DIRTY_OPS_LIMIT = 10  # после стольких ставок/отмен — принудительный ресинк

ASSETS_FILE = os.path.join("data", "assets.json")


async def run_engine(
    client,
    discount_percent: float,
    log,
    is_running_flag,
    target_backdrops: list[str] | None = None,
):
    init_db()

    def log_and_notify(message: str, notify: bool = False) -> None:
        log(message)
        if notify:
            asyncio.create_task(send_telegram_notification(message))

    log_and_notify(
        f"[*] ОРДЕР-ДВИЖОК ЗАПУЩЕН. Целевая скидка: {discount_percent}%. Выходим на ковер...",
        notify=True,
    )

    if not os.path.exists(ASSETS_FILE):
        log_and_notify(
            "[!] Нет базы assets.json. Сначала запустите сканер.", notify=True
        )
        return

    with open(ASSETS_FILE, "r", encoding="utf-8") as f:
        assets = json.load(f)

    too_expensive = set()

    target_backdrops = target_backdrops or []

    dirty_ops = 0

    while is_running_flag():
        # --- Жёсткий ресинк баланса в начале каждого круга ---
        try:
            current_balance = await client.get_balance()
        except Exception as e:
            log_and_notify(f"[!] Ошибка связи с банком: {e}", notify=True)
            await asyncio.sleep(5)
            continue

        log(f"[ENGINE] 💰 Баланс на старте круга: {current_balance:.4f} TON")
        dirty_ops = 0

        affordable_count = 0

        for asset in assets:
            if not is_running_flag():
                break

            if affordable_count >= 50:
                break

            collection = asset.get("name")
            if not collection:
                continue

            if collection in too_expensive:
                continue

            bgs_to_scan = target_backdrops if target_backdrops else [None]

            for bg in bgs_to_scan:
                if not is_running_flag():
                    break

                bg_label = f"[{bg}] " if bg else ""
                bg_list = [bg] if bg else []

                try:
                    lots = await client.get_listings(
                        collection,
                        count=1,
                        ordering="Price",
                        low_to_high=True,
                        backdrop_names=bg_list,
                    )
                    if not lots:
                        log(
                            f"   [!] {collection[:15]:<15} {bg_label}| Сервер не отдал лоты (Пусто или Бан)"
                        )
                        continue

                    floor_nano = (
                        lots[0].get("priceNanoTONs") or lots[0].get("salePrice") or 0
                    )
                    if not floor_nano:
                        log(
                            f"   [!] {collection[:15]:<15} {bg_label}| Лоты получены, но нет цены"
                        )
                        continue

                    multiplier = 1.0 - (discount_percent / 100.0)
                    max_bid_nano = int(floor_nano * multiplier)
                    max_bid_ton = max_bid_nano / 10**9

                    if max_bid_ton > current_balance:
                        log(
                            f"   [skip] {collection[:15]:<15} {bg_label}| Тяжеловес ({max_bid_ton:.2f} TON)."
                        )
                        if not bg:
                            too_expensive.add(collection)
                        continue

                    affordable_count += 1

                    orders = await client.get_collection_orders(
                        collection, backdrop_names=bg_list
                    )
                    my_orders = [o for o in orders if o.get("isMine")]
                    comp_orders = [o for o in orders if not o.get("isMine")]

                    my_active_id = my_orders[0].get("id") if my_orders else None
                    my_bid = my_orders[0].get("priceMaxNanoTONs", 0) if my_orders else 0
                    highest_comp_bid = (
                        comp_orders[0].get("priceMaxNanoTONs", 0) if comp_orders else 0
                    )

                    if my_active_id and my_bid > highest_comp_bid:
                        log(
                            f"   [~] {collection[:15]:<15} {bg_label}| Держим 1-е место: {my_bid/10**9:.3f} TON"
                        )
                        await asyncio.sleep(1.5)
                        continue

                    if highest_comp_bid >= max_bid_nano:
                        msg = (
                            f"   [!] {collection[:15]:<15} {bg_label}| "
                            f"Конкурент ({highest_comp_bid/10**9:.3f}) пробил стоп-кран ({max_bid_ton:.3f})."
                        )
                        log(msg)

                        if my_active_id:
                            cancel_msg = f"   [-] Снимаем свою ставку. Отступаем."
                            log_and_notify(cancel_msg, notify=True)
                            await client.cancel_collection_order(my_active_id)
                            dirty_ops += 1

                            if dirty_ops >= DIRTY_OPS_LIMIT:
                                try:
                                    current_balance = await client.get_balance()
                                    log(
                                        f"[ENGINE] 🔄 Ресинк баланса: {current_balance:.4f} TON"
                                    )
                                except Exception as e:
                                    log(f"[ENGINE] ⚠️ Ресинк баланса упал: {e}")
                                dirty_ops = 0

                        await asyncio.sleep(1.5)
                        continue

                    if highest_comp_bid == 0:
                        new_bid_nano = STEP_FIRST_BID_NANO
                    else:
                        new_bid_nano = highest_comp_bid + STEP_OVERBID_NANO

                    if new_bid_nano > max_bid_nano:
                        new_bid_nano = max_bid_nano

                    new_bid_ton = new_bid_nano / 10**9

                    if new_bid_ton > current_balance:
                        log(
                            f"   [!] {collection[:15]:<15} {bg_label}| Пропуск. Не хватает баланса ({new_bid_ton:.2f} TON)."
                        )
                        continue

                    log(
                        f"   [⚔️] {collection[:15]:<15} {bg_label}| Перебиваем: {new_bid_ton:.3f} TON"
                    )

                    if my_active_id:
                        await client.cancel_collection_order(my_active_id)
                        dirty_ops += 1
                        await asyncio.sleep(0.5)

                    await client.create_collection_order(
                        collection, new_bid_nano, backdrop_name=bg
                    )
                    current_balance -= new_bid_ton
                    dirty_ops += 1

                    # === Запись в Trade Journal: факт постановки ставки ===
                    log_trade(
                        trade_type="bid",
                        collection=collection,
                        price_ton=new_bid_ton,
                        backdrop=bg,
                        extra=f"floor_ton={floor_nano/10**9:.4f};max_bid_ton={max_bid_ton:.4f}",
                    )

                    notify_text = (
                        f"✅ Ставка выставлена\n"
                        f"Коллекция: {collection}\n"
                        f"Фон: {bg if bg else '-'}\n"
                        f"Ставка: {new_bid_ton:.3f} TON\n"
                        f"Флор: {floor_nano / 10**9:.3f} TON\n"
                        f"Лимит: {max_bid_ton:.3f} TON"
                    )
                    log_and_notify(notify_text, notify=True)

                    if dirty_ops >= DIRTY_OPS_LIMIT:
                        try:
                            current_balance = await client.get_balance()
                            log(
                                f"[ENGINE] 🔄 Ресинк баланса: {current_balance:.4f} TON"
                            )
                        except Exception as e:
                            log(f"[ENGINE] ⚠️ Ресинк баланса упал: {e}")
                        dirty_ops = 0

                except Exception as e:
                    log_and_notify(
                        f"   [X] Ошибка ({collection[:10]}): {e}", notify=True
                    )

                await asyncio.sleep(2.5)

        log(
            f"\n[i] Круг завершен. Обработано лотов: {affordable_count}. Восстанавливаем дыхалку 5 сек...\n"
        )
        for _ in range(5):
            if not is_running_flag():
                break
            await asyncio.sleep(1)

    log_and_notify("\n[i] Движок остановлен. Вышли из партера.", notify=True)
