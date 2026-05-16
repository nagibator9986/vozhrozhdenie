from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.domain.entities import Conversation, Document


class IConversationRepository(ABC):
    """Abstract interface for conversation persistence."""

    @abstractmethod
    async def get_conversation(self, user_id: str) -> Optional[Conversation]:
        ...

    @abstractmethod
    async def save_conversation(self, conversation: Conversation) -> None:
        ...

    @abstractmethod
    async def delete_conversation(self, user_id: str) -> None:
        ...


class IAIService(ABC):
    """Abstract interface for AI response generation."""

    @abstractmethod
    async def generate_response(
        self,
        conversation: Conversation,
        context_docs: List[Document],
        system_prompt: str,
        image_data: Optional[bytes] = None,
        image_media_type: str = "image/jpeg",
    ) -> str:
        ...

    @abstractmethod
    async def generate_structured_response(
        self,
        conversation: Conversation,
        context_docs: List[Document],
        system_prompt: str,
        image_data: Optional[bytes] = None,
        image_media_type: str = "image/jpeg",
    ) -> Dict[str, Any]:
        """Generate a structured (tool-use) response for WhatsApp.

        Returns a dict conforming to the respond_to_client schema:
        text, readiness, intent, slots, advance_to_step,
        send_videos, attach_articles, send_reviews_link,
        send_purchase_links, offer_test.
        """
        ...


class IRAGService(ABC):
    """Abstract interface for RAG operations."""

    @abstractmethod
    async def retrieve(self, query: str, k: int = 3) -> List[Document]:
        ...

    @abstractmethod
    async def index_documents(self, force: bool = False) -> None:
        ...
