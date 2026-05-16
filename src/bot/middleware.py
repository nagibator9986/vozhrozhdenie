from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from loguru import logger

from src.config import Settings

# Evict _last_seen entries older than this many seconds to prevent unbounded growth
_CLEANUP_TTL_SECONDS = 3600   # 1 hour
# Run cleanup every N successfully processed messages
_CLEANUP_EVERY_N = 200


class ThrottlingMiddleware(BaseMiddleware):
    """
    Rate-limiting middleware that enforces a minimum interval between messages
    per user.

    Memory-leak fix: the internal ``_last_seen`` dict is periodically compacted —
    entries older than ``_CLEANUP_TTL_SECONDS`` are evicted every
    ``_CLEANUP_EVERY_N`` processed messages, so memory is bounded regardless of
    how many unique users interact with the bot.
    """

    def __init__(self, settings: Settings) -> None:
        self._throttle_rate: float = settings.throttle_rate
        self._last_seen: Dict[int, float] = {}
        self._processed_count: int = 0
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Only throttle Message events (CallbackQuery passes through immediately)
        if not isinstance(event, Message):
            return await handler(event, data)

        user = event.from_user
        if user is None:
            return await handler(event, data)

        user_id = user.id
        now = time.monotonic()
        last = self._last_seen.get(user_id, 0.0)
        elapsed = now - last

        if elapsed < self._throttle_rate:
            logger.debug(
                f"Throttling user {user_id} "
                f"(elapsed {elapsed:.2f}s < {self._throttle_rate}s)"
            )
            try:
                await event.answer(
                    "⏳ Пожалуйста, подождите секунду перед следующим сообщением."
                )
            except Exception as exc:
                logger.warning(f"Could not send throttle notice to {user_id}: {exc}")
            return  # Drop the event — do not call the next handler

        self._last_seen[user_id] = now
        self._processed_count += 1

        # Periodic cleanup to prevent unbounded memory growth
        if self._processed_count % _CLEANUP_EVERY_N == 0:
            self._evict_stale(now)

        return await handler(event, data)

    # ──────────────────────────────────────────────────────────────────────
    # Maintenance
    # ──────────────────────────────────────────────────────────────────────

    def _evict_stale(self, now: float) -> None:
        """Remove entries that have not been active for more than TTL seconds."""
        cutoff = now - _CLEANUP_TTL_SECONDS
        stale = [uid for uid, ts in self._last_seen.items() if ts < cutoff]
        for uid in stale:
            del self._last_seen[uid]
        if stale:
            logger.debug(
                f"ThrottlingMiddleware: evicted {len(stale)} stale entries "
                f"({len(self._last_seen)} remaining)."
            )
