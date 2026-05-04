import os
import re
import json as _json
import asyncio
import aiohttp
from urllib.parse import parse_qs, unquote
from dotenv import load_dotenv

# curl_cffi для обхода Cloudflare TLS-fingerprint
from curl_cffi.requests import AsyncSession as CurlSession

# Pyrogram для авто-авторизации
from pyrogram import Client as TgClient
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName

load_dotenv()

BASE_URL = "https://api.tgmrkt.io/api/v1"

MRKT_BOT_USERNAME = "mrkt"
MRKT_APP_SHORT_NAME = "app"
MRKT_WEB_URL = "https://cdn.tgmrkt.io/"

# Свежий Chrome UA — должен совпадать по major-версии с impersonate ниже
_CHROME_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


class MRKTClient:
    """Асинхронный клиент API маркетплейса MRKT с авто-авторизацией.

    Транспорт:
      - /auth идёт через curl_cffi (impersonate=chrome120) — обход Cloudflare JA3.
      - Остальные запросы — aiohttp (после получения токена + cookie CF не блочит).
    """

    def __init__(
        self,
        token: str | None = None,
        session_name: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
    ):
        self.token = token or os.getenv("MRKT_TOKEN")
        self.session_name = session_name or os.getenv("TG_SESSION_NAME", "mrkt_user")
        self.api_id = api_id or int(os.getenv("TG_API_ID", "0") or 0)
        self.api_hash = api_hash or os.getenv("TG_API_HASH", "")
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
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
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

    def _refresh_session_headers(self):
        if self.session and not self.session.closed:
            self.session._default_headers.update(self.headers)

    # ============================================================
    # Парсинг initData — ключевой момент
    # ============================================================
    @staticmethod
    def _extract_init_data(raw_url: str) -> str | None:
        """
        Достаёт tgWebAppData из web_view.url БЕЗ url-decode.

        Telegram отдаёт URL вида:
            https://cdn.tgmrkt.io/#tgWebAppData=<single-encoded>&tgWebAppVersion=...
        либо иногда:
            https://cdn.tgmrkt.io/?tgWebAppData=...#tgWebAppVersion=...

        MRKT валидирует initData по hash, который рассчитывался от
        SINGLE-encoded строки, поэтому ОБЯЗАТЕЛЬНО возвращаем сырой
        кусок без unquote — корейские jamo (%E1%85%A0), эмодзи и пр.
        должны остаться в percent-encoded форме.
        """
        if not raw_url or not isinstance(raw_url, str):
            return None

        # Соберём кандидатов: всё после '#' и всё после '?'
        candidates = []
        if "#" in raw_url:
            candidates.append(raw_url.split("#", 1)[1])
        if "?" in raw_url:
            candidates.append(raw_url.split("?", 1)[1])
        candidates.append(raw_url)

        for chunk in candidates:
            m = re.search(r"(?:^|[&#?])tgWebAppData=([^&#]+)", chunk)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _parse_init_data_fields(init_data: str) -> dict:
        """
        Декодирует initData ОДИН раз для извлечения служебных полей
        (chat_instance, chat_type, auth_date), которые MRKT хочет
        видеть в payload отдельно. Сам initData при этом не меняем.
        """
        # init_data = "user=%7B...%7D&chat_instance=...&hash=..."
        # После unquote получим читаемые ключи.
        decoded_once = unquote(init_data)
        parsed = parse_qs(decoded_once, keep_blank_values=True)
        out = {k: v[0] for k, v in parsed.items() if v}
        return out

    # ============================================================
    # Авторизация — через curl_cffi
    # ============================================================
    async def authenticate_via_telegram(self):
        session_file = f"{self.session_name}.session"

        if not os.path.exists(session_file):
            raise RuntimeError(
                f"Telegram-сессия '{session_file}' не найдена.\n"
                f"Запустите auth.py один раз для интерактивной авторизации."
            )

        if not self.api_id or not self.api_hash:
            raise RuntimeError("TG_API_ID / TG_API_HASH не заданы в .env.")

        # ---------- 1. web-view url ----------
        async with TgClient(
            self.session_name,
            api_id=self.api_id,
            api_hash=self.api_hash,
            no_updates=True,
        ) as tg:
            bot = await tg.resolve_peer(MRKT_BOT_USERNAME)
            web_view = await tg.invoke(
                RequestAppWebView(
                    peer=bot,
                    app=InputBotAppShortName(
                        bot_id=bot,
                        short_name=MRKT_APP_SHORT_NAME,
                    ),
                    platform="web",
                    write_allowed=True,
                )
            )
            raw_url = web_view.url

        # ---------- 2. парсинг initData ----------
        init_data = self._extract_init_data(raw_url)
        if not init_data:
            preview = raw_url[:200] + ("..." if len(raw_url) > 200 else "")
            raise RuntimeError(f"Не удалось извлечь tgWebAppData. URL: {preview}")

        meta = self._parse_init_data_fields(init_data)

        # ---------- 3. Сборка payload ----------
        # Базовое поле — initData. Остальные поля MRKT вытаскивает сам,
        # но в новых билдах фронт явно их дублирует, поэтому
        # перестраховываемся.
        payload = {
            "initData": init_data,
            "platform": "android",  # совпадает с UA
            "chatType": meta.get("chat_type", "sender"),
            "chatInstance": meta.get("chat_instance", ""),
        }
        # Удаляем пустые значения, чтобы не споткнуться о валидатор
        payload = {k: v for k, v in payload.items() if v not in (None, "")}

        # ---------- 4. Запрос через curl_cffi (обход CF JA3) ----------
        auth_headers = {
            "user-agent": _CHROME_UA,
            "origin": "https://cdn.tgmrkt.io",
            "referer": "https://cdn.tgmrkt.io/",
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9,ru;q=0.8",
            "content-type": "application/json",
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }

        body_text = ""
        status = 0
        try:
            async with CurlSession(impersonate="chrome120") as s:
                r = await s.post(
                    f"{BASE_URL}/auth",
                    json=payload,
                    headers=auth_headers,
                    timeout=30,
                )
                status = r.status_code
                body_text = r.text or ""
        except Exception as e:
            raise RuntimeError(f"Сетевая ошибка при /auth (curl_cffi): {e}") from e

        if status >= 400:
            print("=" * 70)
            print("[AUTH-DEBUG] Ошибка авторизации MRKT")
            print(f"[AUTH-DEBUG] HTTP status     : {status}")
            print(f"[AUTH-DEBUG] raw_url         : {raw_url[:200]}")
            print(f"[AUTH-DEBUG] init_data[:80]  : {init_data[:80]}")
            print(f"[AUTH-DEBUG] init_data len   : {len(init_data)}")
            print(f"[AUTH-DEBUG] payload keys    : {list(payload.keys())}")
            print(f"[AUTH-DEBUG] response body   : {body_text[:400]}")
            print("=" * 70)

            # Фолбэк: если 400 пришёл из-за лишних полей — пробуем «голый» payload
            if status == 400 and len(payload) > 1:
                print("[AUTH-DEBUG] Пробую fallback с минимальным payload {initData}")
                async with CurlSession(impersonate="chrome120") as s:
                    r = await s.post(
                        f"{BASE_URL}/auth",
                        json={"initData": init_data},
                        headers=auth_headers,
                        timeout=30,
                    )
                    status = r.status_code
                    body_text = r.text or ""
                if status >= 400:
                    raise RuntimeError(
                        f"Ошибка авторизации MRKT (fallback): HTTP {status} — {body_text[:200]}"
                    )
            else:
                raise RuntimeError(
                    f"Ошибка авторизации MRKT: HTTP {status} — {body_text[:200]}"
                )

        try:
            data = _json.loads(body_text) if body_text else {}
        except Exception:
            data = {}

        token = data.get("token") or data.get("accessToken") or data.get("access_token")
        if not token:
            raise RuntimeError(f"В ответе /auth нет токена: {data}")

        self.token = token

        # пересоздаём aiohttp-сессию с новым токеном
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        await self.start()

    # ============================================================
    # Универсальный _request — на aiohttp с авто-rotate токена
    # ============================================================
    async def _request(self, method: str, path: str, retries: int = 5, **kwargs):
        if self.session is None or self.session.closed:
            await self.start()

        if not self.token:
            # Если токена нет — пробуем авто-авторизацию
            await self.authenticate_via_telegram()

        url = f"{BASE_URL}{path}"
        attempt = 0
        delay = 1.0
        reauth_done = False

        while True:
            attempt += 1
            try:
                async with self.session.request(method, url, **kwargs) as resp:

                    # 401 → одна автоматическая перевыдача токена
                    if resp.status == 401 and not reauth_done:
                        reauth_done = True
                        print("[AUTH] Токен истёк — перевыпускаю через Telegram...")
                        await self.authenticate_via_telegram()
                        continue

                    if resp.status == 401:
                        raise RuntimeError(
                            "[!] Не удалось обновить токен MRKT даже после reauth."
                        )

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
    # Публичные методы API — БЕЗ ИЗМЕНЕНИЙ
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
        """Стягиваем весь портфель: делает два запроса (холд + витрина) и склеивает результат."""
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

        # 1. Забираем то, что лежит в холде (isListed: False)
        payload_unlisted = base_payload.copy()
        payload_unlisted["isListed"] = False
        try:
            data_unlisted = await self._request("POST", "/gifts", json=payload_unlisted)
            if isinstance(data_unlisted, dict) and "gifts" in data_unlisted:
                all_gifts.extend(data_unlisted["gifts"])
        except Exception as e:
            print(f"[!] Ошибка получения инвентаря (холд): {e}")

        # 2. Забираем то, что стоит на продаже (isListed: True)
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
