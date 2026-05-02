import os
import json
import asyncio

ASSETS_FILE = os.path.join("data", "assets.json")


async def run_engine(
    client,
    discount_percent: float,
    log,
    is_running_flag,
    target_backdrops: list[str] | None = None,
):
    log(
        f"[*] ОРДЕР-ДВИЖОК ЗАПУЩЕН. Целевая скидка: {discount_percent}%. Выходим на ковер..."
    )

    if not os.path.exists(ASSETS_FILE):
        log("[!] Нет базы assets.json. Сначала запустите сканер.")
        return

    with open(ASSETS_FILE, "r", encoding="utf-8") as f:
        assets = json.load(f)

    step_nano = 10_000_000  # Шаг атаки: +0.01 TON
    too_expensive = set()

    # Защита от None
    target_backdrops = target_backdrops or []

    while is_running_flag():
        try:
            current_balance = await client.get_balance()
        except Exception as e:
            log(f"[!] Ошибка связи с банком: {e}")
            await asyncio.sleep(5)
            continue

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

            # Определяем, какие фоны сканировать для этой коллекции.
            # Если фильтр пустой, сканируем 1 раз без фона (None).
            bgs_to_scan = target_backdrops if target_backdrops else [None]

            for bg in bgs_to_scan:
                if not is_running_flag():
                    break

                # Формируем префикс для красивого лога
                bg_label = f"[{bg}] " if bg else ""
                bg_list = [bg] if bg else []

                try:
                    # 1. Запрашиваем флор конкретно по этому цвету
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

                    # Извлекаем цену.
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

                    # ФИЛЬТР БАЛАНСА
                    if max_bid_ton > current_balance:
                        log(
                            f"   [skip] {collection[:15]:<15} {bg_label}| Тяжеловес ({max_bid_ton:.2f} TON)."
                        )
                        # Кидаем в черный список только базовые коллекции, чтобы дорогие фоны не блокировали дешевые
                        if not bg:
                            too_expensive.add(collection)
                        continue

                    affordable_count += 1

                    # 2. Сканируем стакан конкретно по этому цвету
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
                        log(
                            f"   [!] {collection[:15]:<15} {bg_label}| Конкурент ({highest_comp_bid/10**9:.3f}) пробил стоп-кран ({max_bid_ton:.3f})."
                        )
                        if my_active_id:
                            log(f"   [-] Снимаем свою ставку. Отступаем.")
                            await client.cancel_collection_order(my_active_id)
                            current_balance += my_bid / 10**9
                        await asyncio.sleep(1.5)
                        continue

                    new_bid_nano = highest_comp_bid + step_nano
                    if highest_comp_bid == 0:
                        new_bid_nano = int(floor_nano * 0.50)

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
                        current_balance += my_bid / 10**9
                        await asyncio.sleep(0.5)

                    # 3. Делаем ставку с указанием цвета
                    await client.create_collection_order(
                        collection, new_bid_nano, backdrop_name=bg
                    )
                    current_balance -= new_bid_ton

                except Exception as e:
                    log(f"   [X] Ошибка ({collection[:10]}): {e}")

                await asyncio.sleep(2.5)

        log(
            f"\n[i] Круг завершен. Обработано лотов: {affordable_count}. Восстанавливаем дыхалку 5 сек...\n"
        )
        for _ in range(5):
            if not is_running_flag():
                break
            await asyncio.sleep(1)

    log("\n[i] Движок остановлен. Вышли из партера.")
