"""
IdleReminderService — sends a single gentle nudge to a WhatsApp client
who has been silent for ~1 hour after a bot reply.

Design:
- Runs as a background asyncio task inside the aiohttp webhook loop
- Every IDLE_CHECK_INTERVAL seconds scans the conversations DB for eligible
  chats:
    1. Last activity (updated_at) is ≥ IDLE_REMINDER_DELAY_MIN ago
    2. Last activity is ≤ IDLE_REMINDER_DELAY_MAX ago (give-up window)
    3. funnel_step is between STEP_1 and STEP_5 (not NOT_STARTED/COMPLETED/REJECTED)
    4. _reminder_sent flag != "1" — ONLY ONCE per session
    5. operator_mode is not active
    6. Last message role is assistant (we wait for user's turn)
- Sends a soft reminder via WazzupMessenger
- Marks conversation _reminder_sent=1 + updated_at=now to prevent re-send

Once a client replies, any new message advances updated_at, which resets
the idle window. If they answer, perfect — conversation continues. If they
stay silent forever, the single reminder fires once and we stop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterable

from loguru import logger

from src.config import Settings
from src.domain.entities import Conversation, FunnelStep, Role
from src.domain.interfaces import IConversationRepository
from src.transport.base import IMessenger, OutgoingMessage


# ── Tunable constants ─────────────────────────────────────────────────────
IDLE_REMINDER_DELAY_MIN_SECONDS = 60 * 60     # 60 minutes — reminder window opens
IDLE_REMINDER_DELAY_MAX_SECONDS = 8 * 60 * 60  # 8 hours — window closes (too stale)
IDLE_CHECK_INTERVAL_SECONDS = 5 * 60           # scan every 5 minutes

REMINDER_TEXT = (
    "Напомню — я здесь, если у вас остались вопросы по вашему случаю "
    "или по протоколу йодотерапии академика Турлубекова. Готов "
    "продолжить разговор когда удобно — просто напишите, что "
    "вас интересует. 🌿"
)


class IdleReminderService:
    """Periodic scanner that nudges silent WhatsApp clients exactly once."""

    def __init__(
        self,
        repository: IConversationRepository,
        messenger: IMessenger,
        settings: Settings,
    ) -> None:
        self._repo = repository
        self._messenger = messenger
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the background scan loop."""
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run_loop(),
            name="idle-reminder-loop",
        )
        logger.info(
            f"IdleReminderService started: checks every "
            f"{IDLE_CHECK_INTERVAL_SECONDS}s, window "
            f"{IDLE_REMINDER_DELAY_MIN_SECONDS//60}-"
            f"{IDLE_REMINDER_DELAY_MAX_SECONDS//60} min"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Main loop ──────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        try:
            while not self._stopping:
                try:
                    await self._scan_and_remind()
                except Exception as exc:
                    logger.exception(f"IdleReminder scan error: {exc}")
                # Sleep with cancel support
                try:
                    await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    break
        finally:
            logger.info("IdleReminderService loop stopped")

    async def _scan_and_remind(self) -> None:
        """One scan pass."""
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=IDLE_REMINDER_DELAY_MAX_SECONDS)
        window_end = now - timedelta(seconds=IDLE_REMINDER_DELAY_MIN_SECONDS)

        # Prefer a SQL-filtered scan; fall back to list_all only on older
        # repository builds that haven't grown the filter yet.
        convs: list[Conversation] = []
        try:
            if hasattr(self._repo, "list_idle_candidates"):
                convs = await self._repo.list_idle_candidates(  # type: ignore[attr-defined]
                    updated_after=window_start,
                    updated_before=window_end,
                    funnel_min=FunnelStep.STEP_1_FILTER.value,
                    funnel_max=FunnelStep.STEP_5_CLOSING.value,
                )
            elif hasattr(self._repo, "list_all"):
                convs = await self._repo.list_all()  # type: ignore[attr-defined]
            else:
                logger.debug("Repository has no list_all — skipping idle scan")
                return
        except Exception as exc:
            logger.warning(f"IdleReminder: failed to list conversations: {exc}")
            return

        sent_count = 0
        for conv in convs:
            if not self._should_remind(conv, window_start, window_end):
                continue
            try:
                await self._send_reminder(conv)
                sent_count += 1
            except Exception as exc:
                logger.warning(f"IdleReminder: failed for {conv.user_id}: {exc}")

        if sent_count:
            logger.info(f"IdleReminder: sent {sent_count} reminders")

    # ── Eligibility logic ──────────────────────────────────────────────

    def _should_remind(
        self,
        conv: Conversation,
        window_start: datetime,
        window_end: datetime,
    ) -> bool:
        # Only WhatsApp users
        if not conv.user_id.startswith("wa:"):
            return False

        # Already reminded once this session
        if conv.qualification_facts.get("_reminder_sent") == "1":
            return False

        # Operator mode active — stay silent
        op_until = conv.operator_mode_until
        if op_until and op_until > datetime.now(timezone.utc).replace(tzinfo=op_until.tzinfo):
            return False

        # Funnel step must be in active selling range
        if not (
            FunnelStep.STEP_1_FILTER.value
            <= conv.funnel_step.value
            <= FunnelStep.STEP_5_CLOSING.value
        ):
            return False

        # updated_at must fall inside [window_start, window_end]
        updated = conv.updated_at
        if updated is None:
            return False
        # Normalize tz
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if not (window_start <= updated <= window_end):
            return False

        # Last message must be from the bot (we're waiting on client)
        if not conv.messages:
            return False
        last_msg = conv.messages[-1]
        if last_msg.role != Role.ASSISTANT:
            return False

        return True

    # ── Sending ────────────────────────────────────────────────────────

    async def _send_reminder(self, conv: Conversation) -> None:
        """Send the nudge and persist the _reminder_sent flag.

        The save happens against a freshly loaded snapshot, not the stale
        one we used for eligibility. Between the eligibility check and the
        save we made a network call to the messenger; during that window
        the user might have replied — and writing back the stale snapshot
        would clobber that reply. Loading again narrows the race to the
        microseconds between read and write.
        """
        # Extract phone number from "wa:<phone>"
        chat_id = conv.user_id.split(":", 1)[1] if ":" in conv.user_id else conv.user_id

        logger.info(f"IdleReminder: sending to {conv.user_id} (step={conv.funnel_step.value})")
        await self._messenger.send_messages(
            chat_id,
            [OutgoingMessage(text=REMINDER_TEXT, parse_mode=None)],
        )

        # Reload the conversation so we don't clobber any user reply that
        # arrived while the messenger.send_messages() call was in flight.
        fresh = await self._repo.get_conversation(conv.user_id)
        target = fresh if fresh is not None else conv

        # If the user replied during the send, their message is now the
        # last one — sending another reminder later is fine, but we still
        # mark this one as sent so we don't loop on the same idle window.
        target.qualification_facts["_reminder_sent"] = "1"
        target.qualification_facts["_reminder_sent_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        try:
            await self._repo.save_conversation(target)
        except Exception as exc:
            logger.error(f"IdleReminder: failed to persist flag for {conv.user_id}: {exc}")
