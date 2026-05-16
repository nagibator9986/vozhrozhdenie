"""
AnthropicService — Claude (Sonnet 4.5) backend for the AI consultant.

Implements the same IAIService interface as the OpenAI-backed ClaudeService,
but uses Anthropic's native Claude models. Claude Sonnet 4.5 is our production
recommendation for the WhatsApp bot: it follows tool-use schema far more
reliably than gpt-4o-mini, cites RAG content accurately, and produces the
1500-3500 character responses we want without `max_tokens` tricks.

Key behaviours:
- `generate_response` → plain-text output (Telegram legacy path)
- `generate_structured_response` → forced tool use with WHATSAPP_RESPONSE_TOOL
  schema. Returns dict with text/slots/readiness/intent/advance_to_step/
  send_videos/attach_articles/send_reviews_link/send_purchase_links/offer_test.
- RAG docs are injected into the system prompt under a clearly marked
  "НАУЧНЫЙ КОНТЕКСТ" block so the prompt tells the model to cite them.
"""
from __future__ import annotations

import base64
from typing import Any, List, Optional

import anthropic
from loguru import logger

from src.config import Settings
from src.domain.entities import Conversation, Document, Role
from src.domain.interfaces import IAIService


# Claude's tool schema uses the same shape as OpenAI's function schema but
# without the outer "function" wrapper. We mirror the OpenAI tool we built
# in claude_service.py, keeping field names identical so downstream code
# (ConsultantService._process_whatsapp) works with either provider.
ANTHROPIC_RESPOND_TOOL = {
    "name": "respond_to_client",
    "description": (
        "Отправить развёрнутый ответ клиенту в WhatsApp с прикреплением "
        "видео, статей и ссылок. ОБЯЗАТЕЛЬНО заполни text (1500-3500 "
        "символов, с прямыми цитатами из научного контекста), readiness, "
        "intent. Если клиент упомянул проблему — заполни slots. Если шаг "
        "воронки выполнен — advance_to_step. На шагах 2 и 4 ОБЯЗАТЕЛЬНО "
        "прикрепи send_videos. При сомнениях клиента — send_reviews_link. "
        "На шаге 5 или при явном желании купить — send_purchase_links."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Основной текст ответа клиенту. СТРОГО 1500-3500 символов. "
                    "5-8 абзацев. Без markdown. Цитируй дословно в кавычках: "
                    "«по данным Абрахама (2002)...», «профессор Браунштейн "
                    "пишет: ...». Приводи цифры и механизм (NIS-рецепторы, "
                    "йодолактоны, галогены, Wolff-Chaikoff). Заверши живым "
                    "вопросом, продвигающим к следующему шагу воронки. "
                    "НИКОГДА не вставляй URL в текст — URL доставляются "
                    "через send_* флаги."
                ),
            },
            "slots": {
                "type": "object",
                "description": "Извлечённые факты о клиенте (только явно сказанное)",
                "properties": {
                    "diagnosis": {"type": "string"},
                    "duration": {"type": "string"},
                    "imaging_done": {"type": "string"},
                    "prior_treatment": {"type": "string"},
                },
            },
            "readiness": {
                "type": "integer",
                "description": (
                    "Готовность купить 0-10. 0-2 холод/онко, 3-5 интерес, "
                    "6-7 обсуждает, 8-9 готов, 10 просит оплату."
                ),
                "minimum": 0,
                "maximum": 10,
            },
            "intent": {
                "type": "string",
                "enum": ["PRICE", "DETOX", "REVIEWS", "PURCHASE", "NONE"],
            },
            "advance_to_step": {
                "type": ["integer", "null"],
                "description": (
                    "Продвижение воронки 1-6 (только вперёд). null если "
                    "остаёмся на текущем шаге."
                ),
            },
            "send_videos": {
                "type": "array",
                "description": (
                    "До 2 video_key из каталога 'ТВОИ ИНСТРУМЕНТЫ'. "
                    "На шаге 2 (образование) и шаге 4 (презентация) — "
                    "ОБЯЗАТЕЛЬНО как минимум 1 видео."
                ),
                "items": {"type": "string"},
                "maxItems": 2,
            },
            "attach_articles": {
                "type": "array",
                "description": "До 2 article_id из каталога статей.",
                "items": {"type": "string"},
                "maxItems": 2,
            },
            "send_reviews_link": {
                "type": "boolean",
                "description": (
                    "Прислать Yandex Disk с видеоотзывами. Используй когда "
                    "клиент сомневается, просит доказательств, или ты "
                    "упоминаешь реальные результаты."
                ),
            },
            "send_purchase_links": {
                "type": "boolean",
                "description": (
                    "Прислать Kaspi/офис/WhatsApp ссылки. На шаге 5 или "
                    "когда клиент явно готов оформить. НЕ пиши URL в text — "
                    "используй только этот флаг."
                ),
            },
            "offer_test": {
                "type": "boolean",
                "description": (
                    "Предложить бесплатный тест здоровья. Используй когда "
                    "клиент неопределён или не знает что сказать."
                ),
            },
        },
        "required": ["text", "readiness", "intent"],
    },
}


class AnthropicService(IAIService):
    """Anthropic Claude-backed AI service with forced tool use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model
        logger.info(f"AnthropicService ready: model={self._model}")

    # ── Internal helpers ────────────────────────────────────────────────

    def _build_enriched_system(
        self, system_prompt: str, context_docs: List[Document], *, for_tool: bool
    ) -> str:
        if not context_docs:
            return system_prompt

        sorted_docs = sorted(context_docs, key=lambda d: d.score, reverse=True)
        context_text = "\n\n───\n\n".join(
            f"[Фрагмент {i+1} из {doc.metadata.get('source', 'базы знаний')} — "
            f"релевантность {doc.score:.2f}]\n{doc.content}"
            for i, doc in enumerate(sorted_docs)
        )
        tool_note = (
            "\n\nВАЖНО: Ты ОБЯЗАН вызвать инструмент respond_to_client с "
            "заполненными полями. НЕ отвечай простым текстом — только через "
            "вызов инструмента. В поле text пиши развёрнутый ответ "
            "клиенту с дословными цитатами из научного контекста ниже."
            if for_tool
            else ""
        )
        return (
            f"{system_prompt}\n\n"
            f"══════════════════════════════════════════════════════════════════\n"
            f"НАУЧНЫЙ КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ\n"
            f"══════════════════════════════════════════════════════════════════\n"
            f"Ниже — реальные фрагменты статей из нашей научной базы "
            f"(Абрахам, Браунштейн, Флечас, материалы центра). "
            f"ОБЯЗАТЕЛЬНО используй их: цитируй ключевые фразы в кавычках, "
            f"приводи конкретные цифры, имена, годы.\n\n"
            f"{context_text}\n\n"
            f"══════════════════════════════════════════════════════════════════\n"
            f"КОНЕЦ НАУЧНОГО КОНТЕКСТА\n"
            f"══════════════════════════════════════════════════════════════════\n"
            f"{tool_note}"
        )

    def _build_messages(
        self,
        conversation: Conversation,
        image_data: Optional[bytes] = None,
        image_media_type: str = "image/jpeg",
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        recent = conversation.get_recent_messages(self._settings.max_history_messages)
        for i, msg in enumerate(recent):
            is_last_user = (
                msg.role == Role.USER and i == len(recent) - 1
            )
            if msg.role == Role.USER:
                if is_last_user and image_data:
                    # Inject image as vision content block in the last user turn
                    b64 = base64.standard_b64encode(image_data).decode("utf-8")
                    messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": image_media_type,
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": msg.content or "Проанализируй этот медицинский документ/снимок."},
                        ],
                    })
                else:
                    messages.append({"role": "user", "content": msg.content})
            elif msg.role == Role.ASSISTANT:
                messages.append({"role": "assistant", "content": msg.content})
        return messages

    # ── Public API ──────────────────────────────────────────────────────

    async def generate_response(
        self,
        conversation: Conversation,
        context_docs: List[Document],
        system_prompt: str,
        image_data: Optional[bytes] = None,
        image_media_type: str = "image/jpeg",
    ) -> str:
        """Legacy free-form text generation (Telegram path)."""
        system = self._build_enriched_system(system_prompt, context_docs, for_tool=False)
        messages = self._build_messages(conversation, image_data=image_data, image_media_type=image_media_type)

        if not any(m["role"] == "user" for m in messages):
            logger.warning("AnthropicService: no user messages to send")
            return ""

        try:
            result = await self._client.messages.create(
                model=self._model,
                max_tokens=self._settings.max_response_tokens,
                temperature=self._settings.ai_temperature,
                system=system,
                messages=messages,
            )
            # Concatenate text content blocks
            parts: list[str] = []
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    parts.append(block.text)
            return "\n".join(parts).strip()
        except anthropic.APIStatusError as exc:
            logger.error(f"Anthropic API error {exc.status_code}: {exc.message}")
            return "Произошла ошибка при обращении к AI-сервису. Попробуйте ещё раз."
        except anthropic.APIConnectionError as exc:
            logger.error(f"Anthropic connection error: {exc}")
            return "Нет соединения с сервером. Попробуйте позже."
        except Exception as exc:
            logger.exception(f"Anthropic unexpected error: {exc}")
            return "Произошла непредвиденная ошибка. Попробуйте позже."

    async def generate_structured_response(
        self,
        conversation: Conversation,
        context_docs: List[Document],
        system_prompt: str,
        image_data: Optional[bytes] = None,
        image_media_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        """Forced tool use — returns dict conforming to ANTHROPIC_RESPOND_TOOL schema."""
        system = self._build_enriched_system(system_prompt, context_docs, for_tool=True)
        messages = self._build_messages(conversation, image_data=image_data, image_media_type=image_media_type)

        if not any(m["role"] == "user" for m in messages):
            logger.warning("AnthropicService: no user messages to send")
            return {"text": "", "readiness": 0, "intent": "NONE"}

        try:
            result = await self._client.messages.create(
                model=self._model,
                max_tokens=self._settings.max_response_tokens,
                temperature=self._settings.ai_temperature,
                system=system,
                messages=messages,
                tools=[ANTHROPIC_RESPOND_TOOL],
                tool_choice={"type": "tool", "name": "respond_to_client"},
            )

            # Find the first tool_use block
            for block in result.content:
                if getattr(block, "type", None) == "tool_use":
                    args: dict[str, Any] = dict(block.input or {})
                    args.setdefault("text", "")
                    args.setdefault("readiness", 0)
                    args.setdefault("intent", "NONE")
                    args.setdefault("slots", {})
                    args.setdefault("send_videos", [])
                    args.setdefault("attach_articles", [])
                    args.setdefault("send_reviews_link", False)
                    args.setdefault("send_purchase_links", False)
                    args.setdefault("offer_test", False)
                    args.setdefault("advance_to_step", None)
                    return args

            # No tool_use found — fall back to concatenated text
            logger.warning("Anthropic returned no tool_use despite forced tool_choice")
            text_parts = [
                block.text for block in result.content
                if getattr(block, "type", None) == "text"
            ]
            return {
                "text": "\n".join(text_parts).strip(),
                "readiness": 0,
                "intent": "NONE",
            }

        except anthropic.APIStatusError as exc:
            logger.error(f"Anthropic API error {exc.status_code}: {exc.message}")
            return {"text": "Произошла ошибка при обращении к AI-сервису.",
                    "readiness": 0, "intent": "NONE"}
        except anthropic.APIConnectionError as exc:
            logger.error(f"Anthropic connection error: {exc}")
            return {"text": "Нет соединения с сервером. Попробуйте позже.",
                    "readiness": 0, "intent": "NONE"}
        except Exception as exc:
            logger.exception(f"Anthropic unexpected error: {exc}")
            return {"text": "Произошла непредвиденная ошибка.",
                    "readiness": 0, "intent": "NONE"}
