from __future__ import annotations

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from src.transport.base import Button, IMessenger, OutgoingMessage

_TG_MAX_LEN = 4096


def _parse_chat_id(chat_id: str) -> int | str:
    """Strip platform prefix (tg:) and convert to int for Telegram API."""
    if chat_id.startswith("tg:"):
        chat_id = chat_id[3:]
    return int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id


def _buttons_to_inline(buttons: list[Button]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    for btn in buttons:
        if btn.url:
            builder.row(InlineKeyboardButton(text=btn.text, url=btn.url))
        elif btn.callback_data:
            builder.row(InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data))
    return builder.as_markup()


def _split_message(text: str, max_len: int = _TG_MAX_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return parts


class TelegramMessenger(IMessenger):
    """Telegram implementation of IMessenger via aiogram Bot."""

    def __init__(self, bot: Bot, video_service=None) -> None:
        self._bot = bot
        self._video_service = video_service

    async def send_messages(self, chat_id: str, messages: list[OutgoingMessage]) -> None:
        for msg in messages:
            await self._send_one(chat_id, msg)

    async def _send_one(self, chat_id: str, msg: OutgoingMessage) -> None:
        markup = _buttons_to_inline(msg.buttons)
        parts = _split_message(msg.text)
        tg_chat_id = _parse_chat_id(chat_id)

        # Send text parts — attach buttons only to the last part
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            rm = markup if is_last else None
            try:
                await self._bot.send_message(
                    chat_id=tg_chat_id,
                    text=part,
                    parse_mode=msg.parse_mode,
                    reply_markup=rm,
                )
            except Exception:
                try:
                    await self._bot.send_message(
                        chat_id=tg_chat_id,
                        text=part,
                        parse_mode=None,
                        reply_markup=rm,
                    )
                except Exception as exc:
                    logger.error(f"TelegramMessenger: failed to send message to {chat_id}: {exc}")

        # Send video if specified
        if msg.video_url and self._video_service:
            try:
                await self._video_service.send_video(
                    self._bot, tg_chat_id, msg.video_url, caption=None,
                )
            except Exception as exc:
                logger.warning(f"TelegramMessenger: video send failed: {exc}")

        # Send document if specified
        if msg.document_url and msg.document_name:
            try:
                # document_url here is expected to be file bytes or path
                # For now, log a warning — document sending handled directly in handlers
                logger.debug(f"TelegramMessenger: document sending via OutgoingMessage not yet implemented")
            except Exception as exc:
                logger.warning(f"TelegramMessenger: document send failed: {exc}")

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self._bot.send_chat_action(_parse_chat_id(chat_id), "typing")
        except Exception:
            pass

    def max_buttons(self) -> int:
        return 10

    def supports_webapp(self) -> bool:
        return True

    def max_message_length(self) -> int:
        return 4096

    def platform(self) -> str:
        return "telegram"
