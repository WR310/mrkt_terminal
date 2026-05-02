import asyncio
import logging
from typing import Any, Optional
import aiohttp
import os

log = logging.getLogger(__name__)


class MRKTClient:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "MRKTClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session and not self._session.closed:
            return

        token = os.getenv("MRKT_AUTH_TOKEN")
        headers = {
            "authorization": token,
            "cookie": f"access_token={token}",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "origin": "https://cdn.tgmrkt.io",
            "referer": "https://cdn.tgmrkt.io/",
        }

        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)

        self._session = aiohttp.ClientSession(
            base_url="https://api.tgmrkt.io",
            headers=headers,
            timeout=timeout,
            connector=connector,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        assert self._session, "Клиент не запущен"
        async with self._session.request(method, path, **kw) as r:
            if r.status == 429:
                log.warning("Словили 429 Too Many Requests. Ждем...")
                await asyncio.sleep(2)
                return await self._request(method, path, **kw)
            r.raise_for_status()
            return await r.json()

    async def get_balance(self) -> dict:
        return await self._request("GET", "/api/v1/balance")

    async def get_collections(self) -> dict:
        return await self._request("GET", "/api/v1/collections")

    # Сюда мы будем добавлять остальные эндпоинты по мере сборки (ордера, листинги и т.д.)
