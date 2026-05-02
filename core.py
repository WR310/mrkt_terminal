import os
import asyncio
import aiohttp
from urllib.parse import unquote
from dotenv import load_dotenv

# Pyrogram для авто-авторизации
from pyrogram import Client as TgClient
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName, InputUser

load_dotenv()

BASE_URL = "https://api.tgmrkt.io/api/v1"


class MRKTClient:
    """Асинхронный клиент API маркетплейса MRKT с Авто-Авторизацией."""

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("MRKT_TOKEN")
        self.session: aiohttp.ClientSession | None = None

    @property
    def headers(self) -> dict:
        _headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "origin": "https://cdn.tgmrkt.io",
            "referer": "https://cdn.tgmrkt.io/",
        }
        if self.token:
            _headers["authorization"] = self.token
            _headers["cookie"] = f"access_token={self.token}"
        return _headers

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def start(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def authenticate_via_telegram(self):
        """Автоматическое обновление токена через Telegram WebApp."""
        tg_api_id = os.getenv("TG_API_ID")
        tg_api_hash = os.getenv("TG_API_HASH")
        session_name = os.getenv("TG_SESSION", "mrkt_session")

        if not tg_api_id or not tg_api_hash:
            raise ValueError("В .env не найдены ключи TG_API_ID или TG_API_HASH")

        # Получаем данные из Telegram
        async with TgClient(
            session_name, api_id=int(tg_api_id), api_hash=tg_api_hash
        ) as tg_client:
            # Используем безопасный resolve_peer вместо get_users
            peer = await tg_client.resolve_peer("mrkt")

            # Вытаскиваем id и хэш напрямую из структуры peer
            bot = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
            bot_app = InputBotAppShortName(bot_id=bot, short_name="app")

            web_view = await tg_client.invoke(
                RequestAppWebView(
                    peer=peer,
                    app=bot_app,
                    platform="android",
                )
            )

            init_data = unquote(
                web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[
                    0
                ]
            )

        # Отправляем init_data на сервер МРКТ для получения токена
        auth_data = {"data": init_data}
        async with aiohttp.ClientSession() as temp_session:
            async with temp_session.post(f"{BASE_URL}/auth", json=auth_data) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"HTTP {resp.status} - Ошибка выдачи токена МРКТ: {text}"
                    )

                rj = await resp.json()
                new_token = rj.get("token")
                if not new_token:
                    raise RuntimeError("Сервер МРКТ не вернул токен в ответе.")

        # Применяем новый токен и перезапускаем сессию с новыми заголовками
        self.token = new_token
        await self.close()
        await self.start()
        print("[!] Токен успешно обновлен через Telegram.")

    async def _request(
        self,
        method: str,
        path: str,
        retries: int = 5,
        **kwargs,
    ):
        if self.session is None or self.session.closed:
            await self.start()

        # Если токена изначально нет, пытаемся сразу получить его
        if not self.token:
            await self.authenticate_via_telegram()

        url = f"{BASE_URL}{path}"
        attempt = 0
        delay = 1.0

        while True:
            attempt += 1
            try:
                async with self.session.request(method, url, **kwargs) as resp:

                    # ПЕРЕХВАТ 401: Токен истёк
                    if resp.status == 401:
                        if attempt >= retries:
                            raise RuntimeError(
                                "401 Unauthorized: не удалось обновить токен"
                            )
                        await self.authenticate_via_telegram()
                        continue  # Токен обновлён, повторяем текущий запрос

                    # ПЕРЕХВАТ 429: Лимиты
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        if attempt >= retries:
                            raise RuntimeError(
                                f"429 Too Many Requests после {retries} попыток"
                            )
                        await asyncio.sleep(retry_after)
                        delay = min(delay * 2, 30)
                        continue

                    if resp.status >= 400:
                        text = await resp.text()
                        raise RuntimeError(
                            f"HTTP {resp.status} при {method} {path}: {text[:200]}"
                        )

                    return await resp.json(content_type=None)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt >= retries:
                    raise RuntimeError(f"Сетевая ошибка: {e}") from e
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    # === Публичные методы API ===

    async def get_balance(self) -> float:
        """Получает текущий баланс пользователя в TON."""
        try:
            data = await self._request("GET", "/balance")
            # Читаем тот самый JSON, который ты поймал
            if isinstance(data, dict):
                # Берем значение hard (наноТОНы) и переводим в TON
                hard_nano = data.get("hard", 0)
                return hard_nano / 1_000_000_000
            return 0.0
        except Exception as e:
            print(f"[!] Ошибка парсинга баланса: {e}")
            return 0.0

    async def get_collections(self) -> list[dict]:
        data = await self._request("GET", "/gifts/collections")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "collections" in data:
            return data["collections"]
        return []

    async def get_listings(
        self,
        collection_name: str,
        count: int = 1,
        ordering: str = "Price",
        low_to_high: bool = True,
        backdrop_names: list = None,
    ) -> list[dict]:
        """Получает активные лоты (шмотки на витрине) по новому API."""
        payload = {
            "backdropNames": backdrop_names or [],
            "collectionNames": [collection_name],
            "count": count,
            "craftable": None,
            "cursor": "",
            "giftType": None,
            "isCrafted": None,
            "isNew": None,
            "isPremarket": None,
            "isTransferable": None,
            "lowToHigh": low_to_high,
            "luckyBuy": None,
            "maxPrice": None,
            "minPrice": None,
            "modelNames": [],
            "number": None,
            "ordering": ordering,
            "query": None,
            "removeSelfSales": None,
            "symbolNames": [],
            "tgCanBeCraftedFrom": None,
        }
        try:
            # Бьем точно по новому эндпоинту saling
            data = await self._request("POST", "/gifts/saling", json=payload)
            return data.get("gifts", []) if isinstance(data, dict) else []
        except Exception as e:
            error_str = str(e)
            if "DOCTYPE html" in error_str or "502" in error_str:
                print(
                    f"[DEBUG] Cloudflare всё ещё блокирует (502) запросы к {collection_name}"
                )
            else:
                print(f"[!] Ошибка радара витрины: {e}")
            return []

    async def get_collection_orders(
        self, collection_name: str, count: int = 20, backdrop_names: list = None
    ) -> list[dict]:
        """Получает стакан глобальных ордеров (ставок) с учетом фильтра фонов."""
        payload = {
            "backdropNames": backdrop_names or [],
            "collectionNames": [collection_name],
            "count": count,
            "cursor": "",
            "lowToHigh": False,
            "maxPrice": None,
            "minPrice": None,
            "modelNames": [],
            "ordering": "Price",
            "query": None,
            "symbolNames": [],
        }
        try:
            data = await self._request("POST", "/orders", json=payload)
            return data.get("orders", []) if isinstance(data, dict) else []
        except Exception as e:
            print(f"[!] Ошибка радара стакана: {e}")
            return []

    async def create_collection_order(
        self, collection_name: str, price_nano: int, backdrop_name: str = None
    ) -> dict:
        """Создает глобальный оффер на коллекцию (возможно, с конкретным фоном)."""
        payload = {"collectionName": collection_name, "priceMaxNanoTONs": price_nano}
        # Если передан конкретный цвет, добавляем его в запрос
        if backdrop_name:
            payload["backdropName"] = backdrop_name

        return await self._request("POST", "/orders/create", json=payload)

    async def cancel_collection_order(self, order_id: str):
        """Отменяет глобальную ставку по её ID."""
        return await self._request("POST", f"/orders/cancel/{order_id}")

    async def get_active_offers(self) -> list[dict]:
        """Получает список только АКТИВНЫХ офферов пользователя."""
        # Бьем точно по боевому URL с фильтром isActive=true
        data = await self._request("GET", "/activities?offset=0&count=50&isActive=true")

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "activities", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    async def create_collection_order(
        self,
        collection_name: str,
        price_max_nano: int,
        price_min_nano: int = 500000000,
        quantity: int = 1,
    ):
        """Создает глобальный ордер (ставку) на всю коллекцию."""
        payload = {
            "collectionName": collection_name,
            "modelName": None,
            "backdropName": None,
            "symbolName": None,
            "priceMaxNanoTONs": price_max_nano,
            "priceMinNanoTONs": price_min_nano,
            "quantity": quantity,
        }
        return await self._request("POST", "/orders/create", json=payload)

    async def cancel_offer(self, offer_id: str):
        """Отменяет оффер. Передаем ID в теле запроса."""
        payload = {"offerId": offer_id}
        return await self._request("POST", "/offers/cancel", json=payload)

    async def get_inventory(self) -> list[dict]:
        """Запрашивает инвентарь (строго копируя структуру запроса браузера)."""
        payload = {
            "backdropNames": [],
            "collectionNames": [],
            "count": 50,
            "craftable": None,
            "cursor": "",
            "giftType": None,
            "isCrafted": None,
            "isListed": False,
            "isNew": None,
            "isPremarket": None,
            "isTransferable": None,
            "lowToHigh": False,
            "luckyBuy": None,
            "maxPrice": None,
            "minPrice": None,
            "modelNames": [],
            "number": None,
            "ordering": "None",
            "query": None,
            "removeSelfSales": None,
            "symbolNames": [],
            "tgCanBeCraftedFrom": None,
        }
        try:
            data = await self._request("POST", "/gifts", json=payload)

            # Если сервер почему-то вернет ошибку внутри JSON, выводим её в консоль
            if isinstance(data, dict):
                if "gifts" in data:
                    return data["gifts"]
                else:
                    print(
                        f"[?] Неожиданный ответ сервера при запросе инвентаря: {data}"
                    )
            return []
        except Exception as e:
            print(f"[!] Ошибка получения инвентаря: {e}")
            return []

    async def create_offer(self, gift_sale_id: str, price_nano: int) -> dict:
        payload = {"price": int(price_nano), "giftSaleId": gift_sale_id}
        data = await self._request("POST", "/offers/create", json=payload)
        return data if isinstance(data, dict) else {"raw": data}

    async def sell_gifts(self, gift_ids: list[str], prices_nano: list[int]) -> dict:
        """Выставляет предметы на продажу."""
        if len(gift_ids) != len(prices_nano) or not gift_ids:
            raise ValueError("Ошибка входных данных для продажи.")
        payload = {"ids": list(gift_ids), "prices": [int(p) for p in prices_nano]}
        return await self._request("POST", "/gifts/sale", json=payload)

    async def buy_gift(self, gift_id: str, price_nano: int) -> dict | list:
        """Выкупает предмет с рынка. Эндпоинт POST /gifts/buy"""
        payload = {"ids": [gift_id], "prices": {gift_id: int(price_nano)}}
        data = await self._request("POST", "/gifts/buy", json=payload)
        return data if isinstance(data, (dict, list)) else {"raw": data}
