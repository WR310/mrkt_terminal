# core.py
import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.tgmrkt.io/api/v1"

NANO = 1_000_000_000  # 1 TON = 1e9 nanoTON

_CHROME_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

_NO_TOKEN_MSG = (
    "[!] Боезапас пуст: переменная MRKT_TOKEN не задана в .env.\n"
    "    1. Открой MRKT (https://cdn.tgmrkt.io/) в Telegram Web.\n"
    "    2. F12 → Network → фильтр 'api.tgmrkt.io' → скопируй заголовок 'authorization'.\n"
    "    3. Вставь его в .env как  MRKT_TOKEN=<token>\n"
    "    4. Перезапусти терминал."
)

_EXPIRED_TOKEN_MSG = "[!] Токен MRKT истёк (HTTP 401). Срочно обнови MRKT_TOKEN в .env."


def nano_to_ton(value) -> float:
    """
    Единственный канонический конвертер nano → TON.
    Никаких эвристик 'если число большое — значит нано'.
    Все API MRKT возвращают цены в нано-TON, всегда делим на 1e9.
    """
    try:
        return float(value) / NANO
    except (TypeError, ValueError):
        return 0.0


def ton_to_nano(value) -> int:
    try:
        return int(round(float(value) * NANO))
    except (TypeError, ValueError):
        return 0


class MRKTClient:
    """
    Асинхронный клиент API маркетплейса MRKT.
    Авторизация только через MRKT_TOKEN из .env.
    """

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("MRKT_TOKEN")
        self.session: aiohttp.ClientSession | None = None

    @property
    def headers(self) -> dict:
        h = {
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
            h["authorization"] = self.token
            h["cookie"] = f"access_token={self.token}"
        return h

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

    async def _request(self, method: str, path: str, retries: int = 5, **kwargs):
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

    # ---------- Public API ----------
    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/balance")
            if isinstance(data, dict):
                return nano_to_ton(data.get("hard", 0))
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
        backdrop_names: list | None = None,
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
            err = str(e)
            if "DOCTYPE html" in err or "502" in err:
                print(f"[DEBUG] CF блок (502): {collection_name}")
            else:
                print(f"[!] Ошибка радара витрины: {e}")
            return []

    async def get_collection_orders(
        self, collection_name: str, count: int = 20, backdrop_names: list | None = None
    ) -> list[dict]:
        """
        Возвращает Buy Orders (стакан ставок) по коллекции.
        Это и есть источник "Best Bid" для проверки Liquidity Gap.
        """
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

    async def get_collection_offers(self, collection_name: str) -> list[dict]:
        """
        Алиас для семантической ясности: офферы (Buy Orders) — те же ордера.
        Используется scanner.get_best_offer.
        """
        return await self.get_collection_orders(collection_name, count=10)

    async def create_collection_order(
        self,
        collection_name: str,
        price_nano: int,
        backdrop_name: str | None = None,
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
        return await self._request("POST", "/offers/cancel", json={"offerId": offer_id})

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

        all_gifts: list[dict] = []
        for is_listed in (False, True):
            payload = {**base_payload, "isListed": is_listed}
            try:
                data = await self._request("POST", "/gifts", json=payload)
                if isinstance(data, dict) and "gifts" in data:
                    all_gifts.extend(data["gifts"])
            except Exception as e:
                tag = "холд" if not is_listed else "витрина"
                print(f"[!] Ошибка получения инвентаря ({tag}): {e}")
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
