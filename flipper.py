import asyncio

# 1. МУСОР: Сливаем на маркет без сожалений
TRASH_BACKDROPS = ["Standard", "Gradient", "Blue", "Red", "Green", "Purple"]

# 2. ЭЛИТА: Железобетонно прячем в сейф
ELITE_BACKDROPS = ["Black", "Gold", "Satin Gold", "Silver"]

# 3. МОНОХРОМЫ: Жестко заданные связки (Модель + Фон)
ELITE_PAIRS = {
    "Lunar Snake": ["Dark Night", "Black"],
    "Ruby Heart": ["Red", "Crimson"],
    "Golden Apple": ["Gold", "Satin Gold"],
    "Neon Cube": ["Purple", "Toxic Green"],
}

# Список «дженерик»-слов, которые игнорируем при сравнении
GENERIC_WORDS = {
    "the",
    "of",
    "and",
    "a",
    "an",
    "blue",
    "red",
    "green",
    "black",
    "white",
    "gold",
    "silver",
    "light",
    "dark",
    "deep",
    "pale",
    "bright",
}


def is_monochrome_check(model_name: str, backdrop_name: str) -> bool:
    """
    Проверяет, является ли пара (модель, фон) монохромной.
    Монохром = в имени модели и в имени фона есть совпадающий
    значимый токен (НЕ из GENERIC_WORDS).
    """
    if not model_name or not backdrop_name:
        return False

    m_words = set(model_name.lower().split())
    b_words = set(backdrop_name.lower().split())

    for word in b_words:
        if word in GENERIC_WORDS:
            continue
        if len(word) < 3:
            continue
        if word in m_words:
            return True

    return False


async def run_auto_flip(client, log_func):
    log_func("\n[*] Запуск Сортировочного Центра PRO (Монохром + Элита)...")

    try:
        gifts = await client.get_inventory()
        if not gifts:
            log_func("[i] Инвентарь пуст.")
            return

        flipped_ids = []
        flipped_prices = []

        for gift in gifts:
            gift_id = gift.get("id")
            collection = gift.get("collectionName", "Unknown")
            model = gift.get("modelName", "Unknown")
            bg = gift.get("backdropName", "Standard")

            # ШАГ 1: Поиск монохромов
            if is_monochrome_check(model, bg):
                log_func(
                    f"   [💎 МОНОХРОМ] {collection[:12]} | {model} + {bg} -> В СЕЙФ!"
                )
                continue

            # ШАГ 2: Поиск элитных фонов
            if bg in ELITE_BACKDROPS:
                log_func(
                    f"   [🌟 ЭЛИТНЫЙ ФОН] {collection[:12]} | Фон: {bg} -> В СЕЙФ!"
                )
                continue

            # ШАГ 3: Защита от неизвестного
            if bg not in TRASH_BACKDROPS:
                log_func(
                    f"   [🛡️ НЕИЗВЕСТНЫЙ ФОН] {collection[:12]} | Фон: {bg} -> В СЕЙФ!"
                )
                continue

            # ШАГ 4: Слив мусора
            lots = await client.get_listings(collection, count=1, backdrop_names=[bg])
            if not lots:
                continue

            floor_nano = lots[0].get("priceNanoTONs") or lots[0].get("salePrice") or 0
            if floor_nano:
                sell_price = floor_nano - 10_000_000
                flipped_ids.append(gift_id)
                flipped_prices.append(sell_price)
                log_func(
                    f"   [💸 ФЛИП] {collection[:12]} | {bg} -> {sell_price/10**9:.2f} TON"
                )

        if flipped_ids:
            await client.sell_gifts(flipped_ids, flipped_prices)
            log_func(f"\n[✓] Успешно выставлено на маркет: {len(flipped_ids)} шт.")
        else:
            log_func("\n[i] Ничего не подошло под критерии автоматической продажи.")

    except Exception as e:
        log_func(f"[!] Ошибка в модуле флиппера: {e}")
