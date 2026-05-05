# core.py
import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.tgmrkt.io/api/v1"

_CHROME_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)


_NO_TOKEN_MSG = (
    "[!] Боезапас пуст: переменная MRKT_TOKEN не задана в .env.\n"
    "    Как добыть свежий токен:\n"
    "      1. Открой MRKT (https://cdn.tgmrkt.io/) в десктопном Telegram Web (или в браузере).\n"
    "      2. Нажми F12 → вкладка Network → отфильтруй по 'api.tgmrkt.io'.\n"
    "      3. В любом запросе скопируй значение заголовка 'authorization' целиком.\n"
    "      4. Вставь его в .env строкой:  MRKT_TOKEN=<твой_токен>\n"
    "      5. Перезапусти терминал."
)

_EXPIRED_TOKEN_MSG = (
    "[!] Боезапас пуст: токен MRKT истёк (срок жизни ~24 часа), сервер ответил 401.\n"
    "    Срочно обнови MRKT_TOKEN в .env:\n"
    "      1. Открой MRKT (https://cdn.tgmrkt.io/) в Telegram Web.\n"
    "      2. F12 → Network → фильтр 'api.tgmrkt.io' → скопируй заголовок 'authorization'.\n"
    "      3. Замени значение MRKT_TOKEN в .env на свежее.\n"
    "      4. Перезапусти терминал."
)


class MRKTClient:
    """
    Асинхронный клиент API маркетплейса MRKT.

    Авторизация — ТОЛЬКО ручная: токен передаётся через переменную окружения
    MRKT_TOKEN (или аргумент конструктора). Никакой авто-перевыпуск через
    Telegram больше не выполняется — Pyrogram отдаёт устаревший Layer 158,
    а бэкенд MRKT требует Layer 178+ с валидным signature.
    """

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("MRKT_TOKEN")
        self.session: aiohttp.ClientSession | None = None

    # ---------- headers ----------
    @property
    def headers(self) -> dict:
        _headers = {
            "user-agent": _CHROME_UA,
            "origin": "https://cdn.tgmrkt.io",
            "referer": "https://cdn.tgmrkt.io/",
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9,ru;q=0.8",
            "content-type": "application/json",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
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

    # ============================================================
    # Универсальный _request — без авто-авторизации
    # ============================================================
    async def _request(self, method: str, path: str, retries: int = 5, **kwargs):
        # Жёсткий стоп-кран №1: токена нет вообще
        if not self.token:
            raise RuntimeError(_NO_TOKEN_MSG)

        if self.session is None or self.session.closed:
            await self.start()

        url = f"{BASE_URL}{path}"
        attempt = 0
        delay = 1.0

        while True:
            attempt += 1
            try:
                async with self.session.request(method, url, **kwargs) as resp:

                    # Жёсткий стоп-кран №2: токен мёртв
                    if resp.status == 401:
                        raise RuntimeError(_EXPIRED_TOKEN_MSG)

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

    # ============================================================
    # Публичные методы API — без изменений
    # ============================================================
    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/balance")
            print(f"\n[DEBUG-BALANCE] Сырой ответ сервера: {data}\n")
            if isinstance(data, dict):
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
            data = await self._request("POST", "/gifts/saling", json=payload)
            return data.get("gifts", []) if isinstance(data, dict) else []
        except Exception as e:
            error_str = str(e)
            if "DOCTYPE html" in error_str or "502" in error_str:
                print(f"[DEBUG] CF блок (502): {collection_name}")
            else:
                print(f"[!] Ошибка радара витрины: {e}")
            return []

    async def get_collection_orders(
        self, collection_name: str, count: int = 20, backdrop_names: list = None
    ) -> list[dict]:
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
            "lowToHigh": False,
            "luckyBuy": None,
            "maxPrice": None,
            "minPrice": None,
            "modelNames": [],
            "number": None,
            "ordering": "Price",
            "query": None,
            "removeSelfSales": None,
            "symbolNames": [],
            "tgCanBeCraftedFrom": None,
        }
        try:
            data = await self._request("POST", "/orders", json=payload)
            return data.get("orders", []) if isinstance(data, dict) else []
        except Exception as e:
            print(f"[!] Ошибка радара стакана: {e}")
            return []

    async def create_collection_order(
        self,
        collection_name: str,
        price_nano: int,
        backdrop_name: str = None,
        price_min_nano: int = 500_000_000,
        quantity: int = 1,
    ) -> dict:
        payload = {
            "collectionName": str(collection_name),
            "modelName": None,
            "backdropName": str(backdrop_name) if backdrop_name else None,
            "symbolName": None,
            "priceMaxNanoTONs": int(price_nano),
            "priceMinNanoTONs": int(price_min_nano),
            "quantity": int(quantity),
        }
        return await self._request("POST", "/orders/create", json=payload)

    async def cancel_collection_order(self, order_id: str):
        return await self._request("POST", f"/orders/cancel/{order_id}")

    async def get_active_offers(self) -> list[dict]:
        data = await self._request("GET", "/activities?offset=0&count=50&isActive=true")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "activities", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    async def cancel_offer(self, offer_id: str):
        payload = {"offerId": offer_id}
        return await self._request("POST", "/offers/cancel", json=payload)

    async def get_inventory(self) -> list[dict]:
        base_payload = {
            "backdropNames": [],
            "collectionNames": [],
            "count": 50,
            "craftable": None,
            "cursor": "",
            "giftType": None,
            "isCrafted": None,
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

        all_gifts = []

        payload_unlisted = base_payload.copy()
        payload_unlisted["isListed"] = False
        try:
            data_unlisted = await self._request("POST", "/gifts", json=payload_unlisted)
            if isinstance(data_unlisted, dict) and "gifts" in data_unlisted:
                all_gifts.extend(data_unlisted["gifts"])
        except Exception as e:
            print(f"[!] Ошибка получения инвентаря (холд): {e}")

        payload_listed = base_payload.copy()
        payload_listed["isListed"] = True
        try:
            data_listed = await self._request("POST", "/gifts", json=payload_listed)
            if isinstance(data_listed, dict) and "gifts" in data_listed:
                all_gifts.extend(data_listed["gifts"])
        except Exception as e:
            print(f"[!] Ошибка получения инвентаря (витрина): {e}")

        return all_gifts

    async def create_offer(self, gift_sale_id: str, price_nano: int) -> dict:
        payload = {"price": int(price_nano), "giftSaleId": gift_sale_id}
        data = await self._request("POST", "/offers/create", json=payload)
        return data if isinstance(data, dict) else {"raw": data}

    async def sell_gifts(self, gift_ids: list[str], prices_nano: list[int]) -> dict:
        if len(gift_ids) != len(prices_nano) or not gift_ids:
            raise ValueError("Ошибка входных данных для продажи.")
        payload = {"ids": list(gift_ids), "prices": [int(p) for p in prices_nano]}
        return await self._request("POST", "/gifts/sale", json=payload)

    async def buy_gift(self, gift_id: str, price_nano: int) -> dict | list:
        payload = {"ids": [gift_id], "prices": {gift_id: int(price_nano)}}
        data = await self._request("POST", "/gifts/buy", json=payload)
        return data if isinstance(data, (dict, list)) else {"raw": data}
