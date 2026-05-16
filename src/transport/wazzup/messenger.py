from __future__ import annotations

import asyncio
import re
import time

from loguru import logger

from src.transport.base import Button, IMessenger, OutgoingMessage
from src.transport.wazzup.client import WazzupClient

_WA_MAX_LEN = 4096
_WA_BUTTON_MAX_LEN = 20

# Regex to strip emojis (Unicode emoji ranges)
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"  # dingbats
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero width joiner
    "\U00002640-\U00002642"  # gender symbols
    "\U00002600-\U000026FF"  # misc symbols
    "\U0000231A-\U0000231B"  # watch/hourglass
    "\U00002934-\U00002935"  # arrows
    "\U000025AA-\U000025FE"  # geometric shapes
    "\U00002B05-\U00002B07"  # arrows
    "\U00002B1B-\U00002B1C"  # squares
    "\U00002B50"             # star
    "\U00002B55"             # circle
    "\U00003030"             # wavy dash
    "\U0000303D"             # part alt mark
    "\U00003297"             # circled ideograph
    "\U00003299"             # circled ideograph
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002702-\U000027B0"  # dingbats
    "\U0000FE0F"             # variation selector
    "✅❌▶️◀️❓"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Remove emojis from text for WhatsApp button labels."""
    return _EMOJI_RE.sub("", text).strip()


def _clean_button_text(text: str) -> str:
    """Strip emojis and enforce 20-char limit for WhatsApp quick-reply buttons."""
    clean = _strip_emoji(text)
    if len(clean) > _WA_BUTTON_MAX_LEN:
        clean = clean[:_WA_BUTTON_MAX_LEN].rstrip()
    return clean


def _split_message(text: str, max_len: int = _WA_MAX_LEN) -> list[str]:
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


class WazzupMessenger(IMessenger):
    """WhatsApp implementation of IMessenger via Wazzup24 API."""

    def __init__(self, client: WazzupClient, video_base_url: str = "") -> None:
        self._client = client
        self._video_base_url = video_base_url.rstrip("/")

    async def send_messages(self, chat_id: str, messages: list[OutgoingMessage]) -> None:
        """Send a batch of outgoing messages optimally.

        Order: first flush all text-only messages (sequential, fast ~0.5s each),
        then fire all media messages in parallel via asyncio.gather. This gives
        the client an instant text response while videos upload in background.

        Previous implementation was fully sequential — with 2 videos of 5-8 MB
        each on ngrok-free bandwidth, clients waited 15-30s for the full batch.
        Now text lands in <1s and videos arrive asynchronously.
        """
        text_msgs: list[OutgoingMessage] = []
        media_msgs: list[OutgoingMessage] = []

        for msg in messages:
            # Pure-media = empty text + video_url/document_url
            if msg.video_url and not (msg.text and msg.text.strip()):
                media_msgs.append(msg)
            elif msg.video_url:
                # Mixed: text caption + video. Send text first (as part of
                # text phase), then queue the bare video to media phase.
                text_msgs.append(OutgoingMessage(
                    text=msg.text, buttons=msg.buttons, parse_mode=msg.parse_mode,
                ))
                media_msgs.append(OutgoingMessage(
                    text="", video_url=msg.video_url, parse_mode=None,
                ))
            else:
                text_msgs.append(msg)

        # Phase 1 — text messages strictly in order, sequentially
        for msg in text_msgs:
            await self._send_one(chat_id, msg)

        # Phase 2 — media messages in parallel (fire, but await all for
        # error visibility; they run concurrently so total time = slowest)
        if media_msgs:
            t0 = time.time()
            results = await asyncio.gather(
                *(self._send_media_only(chat_id, m) for m in media_msgs),
                return_exceptions=True,
            )
            dt = time.time() - t0
            failures = [r for r in results if isinstance(r, Exception)]
            logger.info(
                f"Wazzup media batch: {len(media_msgs)} items in {dt:.1f}s"
                + (f" ({len(failures)} failed)" if failures else "")
            )

    async def _send_media_only(self, chat_id: str, msg: OutgoingMessage) -> None:
        """Send exactly one video/document without any text."""
        if not msg.video_url:
            return
        video_url = msg.video_url
        if not video_url.startswith("http") and self._video_base_url:
            video_url = f"{self._video_base_url}/{video_url}"
        try:
            t0 = time.time()
            await self._client.send_file(chat_id, video_url)
            logger.debug(f"Wazzup video sent in {time.time()-t0:.1f}s: {video_url[-40:]}")
        except Exception as exc:
            logger.warning(f"Wazzup video send failed: {exc}")

    async def _send_one(self, chat_id: str, msg: OutgoingMessage) -> None:
        text = msg.text

        # Separate URL buttons from callback buttons
        url_buttons = [b for b in msg.buttons if b.url]
        action_buttons = [b for b in msg.buttons if b.callback_data]

        # Append URL links to message text (WhatsApp quick-reply doesn't support URLs)
        if url_buttons:
            text += "\n"
            for btn in url_buttons:
                text += f"\n🔗 {btn.text}: {btn.url}"

        parts = _split_message(text)

        # Build quick-reply buttons from callback buttons ONLY (max 3)
        wa_buttons: list[dict[str, str]] | None = None
        if action_buttons:
            wa_buttons = []
            for btn in action_buttons[:3]:
                clean_text = _clean_button_text(btn.text)
                if clean_text:
                    wa_buttons.append({"text": clean_text, "type": "text"})

            if not wa_buttons:
                wa_buttons = None

        # If >3 action buttons, add remaining as numbered text
        if len(action_buttons) > 3:
            overflow_text = "\n\nИли напишите номер:\n"
            for i, btn in enumerate(action_buttons[3:], start=4):
                overflow_text += f"{i}. {_strip_emoji(btn.text)}\n"
            parts[-1] += overflow_text

        # Send text parts — attach buttons only to the last part.
        # Skip sending entirely if the message has no text and only a media
        # payload (video_url) — avoid posting empty WhatsApp messages.
        has_text = bool(text.strip())
        if has_text:
            for i, part in enumerate(parts):
                is_last = i == len(parts) - 1
                btns = wa_buttons if is_last else None
                try:
                    await self._client.send_message(chat_id, part, buttons=btns)
                except Exception as exc:
                    logger.error(f"WazzupMessenger: failed to send to {chat_id}: {exc}")
                    # Retry without buttons if button was the cause
                    if btns:
                        try:
                            await self._client.send_message(chat_id, part, buttons=None)
                        except Exception as exc2:
                            logger.error(f"WazzupMessenger: retry without buttons also failed: {exc2}")

        # Send video if specified
        if msg.video_url:
            video_url = msg.video_url
            if not video_url.startswith("http") and self._video_base_url:
                video_url = f"{self._video_base_url}/{video_url}"
            try:
                await self._client.send_file(chat_id, video_url)
            except Exception as exc:
                logger.warning(f"WazzupMessenger: video send failed: {exc}")

    async def send_typing(self, chat_id: str) -> None:
        pass

    def max_buttons(self) -> int:
        return 3

    def supports_webapp(self) -> bool:
        return False

    def max_message_length(self) -> int:
        return 4096

    def platform(self) -> str:
        return "whatsapp"
