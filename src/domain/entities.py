from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    """Naive UTC datetime (replaces deprecated datetime.utcnow()).

    We keep datetimes naive (without tzinfo) for consistency with existing
    SQLite DateTime columns which strip timezone info on round-trip.
    All datetimes in this codebase are UTC — just not explicitly marked.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class FunnelStep(int, Enum):
    """Tracks where a user is in the static sales funnel."""
    NOT_STARTED = 0
    STEP_1_FILTER = 1          # Приветствие-фильтр
    STEP_2_EDUCATION = 2       # Образование (сжатое)
    STEP_3_QUALIFICATION = 3   # Квалификация (AI спрашивает)
    STEP_4_PRESENTATION = 4    # Персональная презентация
    STEP_5_CLOSING = 5         # Закрытие (Kaspi/Офис/WhatsApp)
    COMPLETED = 6              # Воронка пройдена → режим AI-ответов
    REJECTED = -1              # Нажал «Не нужно»


@dataclass
class Message:
    role: Role
    content: str
    timestamp: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            role=Role(data["role"]),
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


@dataclass
class Conversation:
    user_id: str
    messages: list[Message] = field(default_factory=list)
    funnel_step: FunnelStep = FunnelStep.NOT_STARTED
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    # WhatsApp-specific state (persisted for all platforms but primarily used by WA flow)
    pending_action: str | None = None           # e.g. "test_offer"
    pending_action_expires_at: datetime | None = None
    operator_mode_until: datetime | None = None  # Bot silent until this time
    qualification_facts: dict[str, Any] = field(default_factory=dict)
    stall_counter: int = 0                       # off-topic questions in a row
    objections_handled: list[str] = field(default_factory=list)  # ["price", "doctor", ...]
    readiness_score: int = 0                     # 0-10, updated by LLM

    def add_message(self, role: Role, content: str) -> None:
        """Add a message to the conversation history."""
        self.messages.append(Message(role=role, content=content))
        self.updated_at = _utcnow()

    def get_recent_messages(self, limit: int = 20) -> list[Message]:
        """Return the most recent messages up to the given limit."""
        return self.messages[-limit:] if len(self.messages) > limit else self.messages

    def is_in_operator_mode(self) -> bool:
        """Check if bot should be silent because operator is handling this chat."""
        if self.operator_mode_until is None:
            return False
        return _utcnow() < self.operator_mode_until

    def has_pending_action(self) -> bool:
        """Check if bot is waiting for a specific answer (e.g. test offer)."""
        if self.pending_action is None:
            return False
        if self.pending_action_expires_at is None:
            return False  # No expiry set means not tracked
        return _utcnow() < self.pending_action_expires_at

    def clear_pending_action(self) -> None:
        self.pending_action = None
        self.pending_action_expires_at = None


@dataclass
class Document:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
