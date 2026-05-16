"""Escalation service — hand off WhatsApp conversation to a human operator.

Simple, realistic implementation:
- Marks conversation as operator_mode (bot stops replying) for N hours
- Logs escalation to a file (data/escalations.log) for team notification
- Auto-releases after timeout

Does NOT use Wazzup's programmatic chat assignment (not researched, fragile).
Operator handles the chat manually via Wazzup Web UI — writing from the same
WhatsApp number, so user never switches channels.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import aiofiles
from loguru import logger

from src.domain.entities import Conversation, _utcnow


# Reasons for escalation — used in logs and optionally in notification
class EscalationReason:
    USER_REQUEST = "user_request"          # User explicitly asked for a human
    MEDIA_UPLOAD = "media_upload"          # User sent photo/document (e.g., UZI)
    STALL = "stall"                        # 3+ off-topic questions in a row
    HOSTILE = "hostile"                    # Negative tone detected
    HIGH_INTENT = "high_intent"            # READY score >= 8 + purchase questions
    SLASH_COMMAND = "slash_command"        # Direct /operator command
    SERIOUS_DIAGNOSIS = "serious_diagnosis"  # Cancer / chemo / tumor — MD required


# Keywords that indicate user wants to talk to a human (positive triggers)
HUMAN_REQUEST_KEYWORDS = (
    "человек", "живой человек", "оператор", "менеджер",
    "консультант", "специалист", "с человеком", "к человеку",
    "живого", "с живым", "настоящий", "реальный",
)

# Phrases that NEGATE the request (user does NOT want a human right now)
HUMAN_REQUEST_NEGATIONS = (
    "не хочу", "не нужен", "не нужна", "не нужно", "не надо",
    "без человек", "без оператор", "без менеджер", "без консультант",
    "потом", "позже", "не сейчас",
)

# Phrases that complain about the bot/consultant (not a genuine request)
HUMAN_REQUEST_COMPLAINTS = (
    "плохо работает", "плохой", "ужасный", "тупой", "надоел",
    "раздражает", "бесполезный", "не помогает",
)

# Serious medical conditions that require IMMEDIATE escalation to a doctor.
# The bot MUST NOT attempt to sell the product to such clients — legal/ethical risk.
SERIOUS_DIAGNOSIS_KEYWORDS = (
    "рак",           # cancer
    "онколог",       # oncology
    "химиотерапи",   # chemotherapy
    "лучев",         # radiation therapy (лучевая/лучевую/лучевой)
    "метастаз",      # metastasis
    "опухол",        # tumor
    "карцином",      # carcinoma
    "саркома",       # sarcoma
    "лимфом",        # lymphoma
    "меланом",       # melanoma
    "лейкоз",        # leukemia
    "4 стади",       # stage 4
    "4-й стади",
    "четверт стади",
    "3 стади",       # stage 3
    "3-й стади",
    "третья стади",
)


def detect_serious_diagnosis(text: str) -> bool:
    """Detect if user mentions cancer/oncology/chemo — requires MD consultation.

    Uses substring matching with word-stems for Russian inflection tolerance.
    Examples:
      'у меня рак груди' → True
      'делаю химиотерапию' → True
      'прошла лучевую' → True
      'интересуюсь йодотерапией' → False (no keywords)
    """
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in SERIOUS_DIAGNOSIS_KEYWORDS)


def detect_human_request(text: str) -> bool:
    """Detect user asking for a human operator.

    Uses keyword matching with negative context detection:
    - Positive keywords must appear
    - Negation keywords (in same message) suppress the match
    - Complaints about consultant also suppress
    """
    lower = text.lower().strip()
    if not lower:
        return False

    # Must contain at least one positive keyword
    matched_kw = next((kw for kw in HUMAN_REQUEST_KEYWORDS if kw in lower), None)
    if not matched_kw:
        return False

    # Check negations: if ANY negation appears before the keyword, suppress
    kw_idx = lower.rfind(matched_kw)
    for neg in HUMAN_REQUEST_NEGATIONS:
        neg_idx = lower.rfind(neg)
        if neg_idx != -1 and neg_idx < kw_idx and (kw_idx - neg_idx) < 30:
            # Negation within 30 chars BEFORE the keyword — suppress
            return False

    # Check complaints: if any complaint phrase appears, user is venting, not requesting
    for complaint in HUMAN_REQUEST_COMPLAINTS:
        if complaint in lower:
            return False

    return True


class EscalationService:
    """Manages conversation handoff to human operators."""

    # Default operator mode duration — bot stays silent this long
    DEFAULT_DURATION_HOURS = 4

    def __init__(self, log_path: str | Path = "data/escalations.log") -> None:
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    async def escalate(
        self,
        conversation: Conversation,
        reason: str,
        duration_hours: int | None = None,
    ) -> None:
        """Mark conversation as escalated and log the event.

        Caller must still call consultant.save_conversation(conversation) afterward
        to persist the operator_mode_until field.
        """
        hours = duration_hours or self.DEFAULT_DURATION_HOURS
        now = _utcnow()
        expires_at = now + timedelta(hours=hours)

        conversation.operator_mode_until = expires_at

        # Build log entry for team notification
        recent_messages = []
        for msg in conversation.messages[-6:]:
            recent_messages.append({
                "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                "content": msg.content[:300],
            })

        entry = {
            "timestamp": now.isoformat() + "Z",  # Explicit UTC marker for log analysis
            "event": "escalation",
            "user_id": conversation.user_id,
            "reason": reason,
            "operator_mode_until": expires_at.isoformat() + "Z",
            "funnel_step": conversation.funnel_step.value,
            "qualification_facts": {
                k: v for k, v in conversation.qualification_facts.items()
                if not k.startswith("_")
            },
            "readiness_score": conversation.readiness_score,
            "recent_messages": recent_messages,
        }

        try:
            async with aiofiles.open(self._log_path, mode="a", encoding="utf-8") as f:
                await f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error(f"Failed to write escalation log: {exc}")

        logger.warning(
            f"ESCALATION: user={conversation.user_id} reason={reason} "
            f"until={expires_at.isoformat()} ready={conversation.readiness_score} "
            f"facts={conversation.qualification_facts}"
        )

    def is_active(self, conversation: Conversation) -> bool:
        """Check if bot should stay silent for this conversation."""
        return conversation.is_in_operator_mode()

    async def release(self, conversation: Conversation) -> None:
        """Manually release a conversation from operator mode (bot resumes)."""
        conversation.operator_mode_until = None
        logger.info(f"Released operator mode for {conversation.user_id}")

    def should_escalate_text(self, text: str, conversation: Conversation) -> str | None:
        """Determine if a text message should trigger escalation.

        Returns the reason string if escalation is needed, None otherwise.
        """
        # Priority 1: explicit user request
        if detect_human_request(text):
            return EscalationReason.USER_REQUEST

        # Priority 2: stall counter reached threshold
        if conversation.stall_counter >= 3:
            return EscalationReason.STALL

        # Priority 3: high intent + complex question (we can't easily detect this
        # without LLM analysis — leave it to explicit intent flagging elsewhere)

        return None
