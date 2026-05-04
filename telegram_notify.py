import os
import aiohttp

_telegram_session: aiohttp.ClientSession | None = None


async def _get_telegram_session() -> aiohttp.ClientSession:
    global _telegram_session

    if _telegram_session is None or _telegram_session.closed:
        timeout = aiohttp.ClientTimeout(total=10)
        _telegram_session = aiohttp.ClientSession(timeout=timeout)

    return _telegram_session


async def send_telegram_notification(text: str) -> bool:
    """
    Асинхронная отправка сообщения в Telegram через прямой POST к Bot API.
    Токен и chat_id берутся только из переменных окружения.
    """
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")

    if not token or not chat_id or not text:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    try:
        session = await _get_telegram_session()

        async with session.post(url, json=payload) as response:
            if response.status != 200:
                body = await response.text()
                print(f"[TG] Ошибка отправки: HTTP {response.status} | {body[:300]}")
                return False

            data = await response.json()
            return bool(data.get("ok"))

    except Exception as e:
        print(f"[TG] Исключение при отправке: {e}")
        return False


async def close_telegram_session() -> None:
    global _telegram_session

    if _telegram_session is not None and not _telegram_session.closed:
        await _telegram_session.close()
        _telegram_session = None
