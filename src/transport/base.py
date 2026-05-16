from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Button:
    """Platform-agnostic button definition."""
    text: str
    callback_data: str | None = None
    url: str | None = None


@dataclass
class OutgoingMessage:
    """Platform-agnostic outgoing message."""
    text: str
    buttons: list[Button] = field(default_factory=list)
    parse_mode: str | None = "Markdown"
    video_url: str | None = None
    document_url: str | None = None
    document_name: str | None = None


class IMessenger(ABC):
    """Abstract messenger interface for multi-channel support."""

    @abstractmethod
    async def send_messages(self, chat_id: str, messages: list[OutgoingMessage]) -> None:
        """Send one or more messages to a chat."""
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        ...

    @abstractmethod
    def max_buttons(self) -> int:
        """Max buttons per message (Telegram=10, WhatsApp=3)."""
        ...

    @abstractmethod
    def supports_webapp(self) -> bool:
        """Whether platform supports WebApp (Telegram=True, WhatsApp=False)."""
        ...

    @abstractmethod
    def max_message_length(self) -> int:
        """Max characters per message."""
        ...

    @abstractmethod
    def platform(self) -> str:
        """Platform name: 'telegram' or 'whatsapp'."""
        ...
