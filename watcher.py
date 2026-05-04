import asyncio
from telegram_notify import send_telegram_notification


async def run_watcher(client, is_running_flag, check_interval: int = 30):
    """
    Фоновый наблюдатель за инвентарём.

    При старте делает первый "слепок" (set из id текущего инвентаря).
    Затем в цикле каждые `check_interval` секунд запрашивает инвентарь снова.
    Если появляются новые id — формирует сообщение и шлёт в Telegram.
    """

    # === 1. Первый слепок инвентаря ===
    try:
        initial_inventory = await client.get_inventory()
    except Exception as e:
        print(f"[WATCHER] Не удалось получить начальный инвентарь: {e}")
        initial_inventory = []

    known_ids: set[str] = {
        item["id"]
        for item in initial_inventory
        if isinstance(item, dict) and item.get("id")
    }

    print(
        f"[WATCHER] Старт. В инвентаре уже {len(known_ids)} предметов. "
        f"Интервал опроса: {check_interval}с."
    )

    # === 2. Основной цикл наблюдения ===
    while is_running_flag():
        try:
            await asyncio.sleep(check_interval)
        except asyncio.CancelledError:
            print("[WATCHER] Получена отмена задачи. Выход.")
            raise

        if not is_running_flag():
            break

        try:
            current_inventory = await client.get_inventory()
        except Exception as e:
            print(f"[WATCHER] Ошибка при запросе инвентаря: {e}")
            continue

        if not current_inventory:
            continue

        current_by_id: dict[str, dict] = {
            item["id"]: item
            for item in current_inventory
            if isinstance(item, dict) and item.get("id")
        }
        current_ids: set[str] = set(current_by_id.keys())

        # === 3. Поиск новых id ===
        new_ids = current_ids - known_ids

        if new_ids:
            print(f"[WATCHER] 🎯 Обнаружено новых предметов: {len(new_ids)}")

            for gift_id in new_ids:
                item = current_by_id.get(gift_id, {})
                collection = item.get("collectionName") or "—"
                backdrop = item.get("backdropName") or "—"

                msg = (
                    "✅ Покупка успешна!\n"
                    f"Коллекция: {collection}\n"
                    f"Фон: {backdrop}\n"
                    f"ID: {gift_id}"
                )

                asyncio.create_task(send_telegram_notification(msg))

        # === 4. Обновляем слепок ===
        known_ids = current_ids

    print("[WATCHER] Флаг is_running_flag() == False. Наблюдатель остановлен.")
