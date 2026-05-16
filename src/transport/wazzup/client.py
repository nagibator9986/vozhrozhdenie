from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiohttp
import certifi
from loguru import logger

# Errors file for API failures (separate from main bot.log)
_ERROR_LOG = "data/wazzup_errors.log"


class WazzupClient:
    """Async HTTP client for Wazzup24 REST API v3 with retry logic."""

    BASE_URL = "https://api.wazzup24.com/v3"
    MAX_RETRIES = 2          # 1 initial attempt + 2 retries = 3 total
    RETRY_DELAY = 2.0        # seconds, doubled on each retry
    TIMEOUT_SECONDS = 15

    def __init__(self, api_key: str, channel_id: str) -> None:
        self._api_key = api_key
        self._channel_id = channel_id
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def _post_with_retry(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to Wazzup with retry-on-5xx + exponential backoff on 429."""
        session = await self._get_session()
        url = f"{self.BASE_URL}{endpoint}"
        last_error: dict[str, Any] = {"error": "all retries failed", "status": 0}

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with session.post(url, json=payload) as resp:
                    status = resp.status

                    if status == 201:
                        try:
                            return await resp.json()
                        except Exception:
                            return {"ok": True}

                    body = await resp.text()

                    # 4xx client errors — don't retry (bad request)
                    if 400 <= status < 500 and status != 429:
                        logger.error(f"Wazzup 4xx error ({status}): {body}")
                        self._log_error(status, body, payload)
                        return {"error": body, "status": status}

                    # 429 rate limit — exponential backoff
                    if status == 429:
                        if attempt < self.MAX_RETRIES:
                            wait = self.RETRY_DELAY * (2 ** attempt)
                            logger.warning(f"Wazzup 429 rate limited, waiting {wait}s")
                            await asyncio.sleep(wait)
                            continue

                    # 5xx server errors — retry with linear backoff
                    if status >= 500:
                        if attempt < self.MAX_RETRIES:
                            wait = self.RETRY_DELAY * (attempt + 1)
                            logger.warning(f"Wazzup 5xx error ({status}), retrying in {wait}s")
                            await asyncio.sleep(wait)
                            continue

                    last_error = {"error": body, "status": status}

            except asyncio.TimeoutError:
                logger.error(f"Wazzup timeout on attempt {attempt + 1}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
                    continue
                last_error = {"error": "timeout", "status": 0}

            except aiohttp.ClientError as exc:
                logger.error(f"Wazzup client error: {exc}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
                    continue
                last_error = {"error": str(exc), "status": 0}

        self._log_error(last_error.get("status", 0), last_error.get("error", ""), payload)
        return last_error

    def _log_error(self, status: int, body: str, payload: dict[str, Any]) -> None:
        """Append error to a dedicated Wazzup error log (non-blocking best-effort).

        Uses blocking open() intentionally — this is a fire-and-forget error log
        called only on API failures (rare), not on every message. Aiofiles overhead
        not justified for a 1-line append that happens once every N hours.
        """
        try:
            import json
            from src.domain.entities import _utcnow
            entry = {
                "timestamp": _utcnow().isoformat() + "Z",
                "status": status,
                "body": body[:500],
                "payload_chat_id": payload.get("chatId"),
                "payload_snippet": str(payload.get("text", ""))[:200],
            }
            with open(_ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # never let logging break the flow

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Send a text message, optionally with quick-reply buttons (max 3)."""
        payload: dict[str, Any] = {
            "channelId": self._channel_id,
            "chatType": "whatsapp",
            "chatId": chat_id,
            "text": text,
        }
        if buttons:
            payload["buttonsObject"] = {"buttons": buttons[:3]}

        result = await self._post_with_retry("/message", payload)
        if "error" not in result:
            logger.debug(f"Wazzup message sent to {chat_id}")
        return result

    async def send_file(
        self,
        chat_id: str,
        content_uri: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Send a file/image/video via public URL."""
        payload: dict[str, Any] = {
            "channelId": self._channel_id,
            "chatType": "whatsapp",
            "chatId": chat_id,
            "contentUri": content_uri,
        }
        result = await self._post_with_retry("/message", payload)
        if "error" not in result:
            logger.debug(f"Wazzup file sent to {chat_id}")
            # Caption goes in a separate message (Wazzup cannot combine text + media)
            if caption:
                await self.send_message(chat_id, caption)
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
