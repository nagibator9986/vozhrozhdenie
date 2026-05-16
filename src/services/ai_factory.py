"""
AI provider factory.

Selects the AI service implementation based on Settings.ai_provider:
- "anthropic" → AnthropicService (Claude Sonnet 4.5 by default — recommended)
- "openai"   → ClaudeService (OpenAI-backed, misnamed for historical reasons)

Both implement the IAIService interface and support both plain
generate_response (legacy Telegram path) and generate_structured_response
(forced tool use for WhatsApp).
"""
from __future__ import annotations

from loguru import logger

from src.config import Settings
from src.domain.interfaces import IAIService


def build_ai_service(settings: Settings) -> IAIService:
    provider = (settings.ai_provider or "anthropic").lower().strip()

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            logger.warning(
                "ai_provider=anthropic but ANTHROPIC_API_KEY is empty — "
                "falling back to OpenAI"
            )
            provider = "openai"
        else:
            from src.services.anthropic_service import AnthropicService
            logger.info(
                f"AI provider: Anthropic Claude ({settings.anthropic_model})"
            )
            return AnthropicService(settings=settings)

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when ai_provider=openai"
            )
        from src.services.claude_service import ClaudeService
        logger.info(f"AI provider: OpenAI ({settings.model_name})")
        return ClaudeService(settings=settings)

    raise RuntimeError(
        f"Unknown ai_provider '{provider}' — expected 'anthropic' or 'openai'"
    )
