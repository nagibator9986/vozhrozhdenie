from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, List, Optional

import openai
from loguru import logger

from src.config import Settings
from src.domain.entities import Conversation, Document, Role
from src.domain.interfaces import IAIService


# JSON schema that the WhatsApp LLM MUST conform to. Enforced via OpenAI
# function calling — LLM cannot return anything that doesn't fit this shape.
WHATSAPP_RESPONSE_TOOL = {
    "type": "function",
    "function": {
        "name": "respond_to_client",
        "description": (
            "Отправить развёрнутый ответ клиенту в WhatsApp. "
            "Обязательно: text (1500-3500 символов, с цитатами из базы знаний, "
            "вопросом в конце), readiness (0-10), intent. "
            "Опционально: slots (извлечённые факты), advance_to_step (продвижение "
            "воронки), send_videos (до 2 видео), attach_articles (до 2 статей), "
            "send_reviews_link, send_purchase_links, offer_test."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Основной текст ответа клиенту. ОБЯЗАТЕЛЬНО: "
                        "1500-3500 символов, 5-8 абзацев, с прямыми цитатами "
                        "и цифрами из научного контекста (Абрахам, Браунштейн, "
                        "Флечас, Нузум), объяснением механизма (NIS-рецепторы, "
                        "йодолактоны, галогены), заканчивается живым вопросом "
                        "ведущим к следующему шагу воронки. БЕЗ markdown, без *_ символов."
                    ),
                },
                "slots": {
                    "type": "object",
                    "description": "Извлечённые факты о клиенте (только то, что он явно сказал)",
                    "properties": {
                        "diagnosis": {"type": "string", "description": "Точный диагноз клиента"},
                        "duration": {"type": "string", "description": "Как давно обнаружили (годы/месяцы)"},
                        "imaging_done": {"type": "string", "description": "Результат УЗИ/маммографии/анализов"},
                        "prior_treatment": {"type": "string", "description": "Что уже пробовали"},
                    },
                    "additionalProperties": False,
                },
                "readiness": {
                    "type": "integer",
                    "description": (
                        "Готовность клиента купить от 0 до 10. "
                        "0-2 холодный/онко, 3-5 интересуется, 6-7 обсуждает, "
                        "8-9 готов, 10 просит оплату."
                    ),
                    "minimum": 0,
                    "maximum": 10,
                },
                "intent": {
                    "type": "string",
                    "enum": ["PRICE", "DETOX", "REVIEWS", "PURCHASE", "NONE"],
                    "description": "Намерение клиента",
                },
                "advance_to_step": {
                    "type": ["integer", "null"],
                    "description": (
                        "Продвинуть воронку на шаг N (1-6) ТОЛЬКО если "
                        "цель текущего шага выполнена. Если оставаться — null. "
                        "Только вперёд, никогда назад. "
                        "1=фильтр, 2=образование, 3=квалификация, "
                        "4=презентация, 5=закрытие, 6=постпродажа."
                    ),
                    "minimum": 1,
                    "maximum": 6,
                },
                "send_videos": {
                    "type": "array",
                    "description": (
                        "До 2 video_key из каталога инструментов (см. "
                        "'ТВОИ ИНСТРУМЕНТЫ'). Прикладывай когда визуальное "
                        "доказательство усилит текст. На шагах 2 и 4 — ОБЯЗАТЕЛЬНО."
                    ),
                    "items": {"type": "string"},
                    "maxItems": 2,
                },
                "attach_articles": {
                    "type": "array",
                    "description": (
                        "До 2 article_id из каталога статей. "
                        "Прикладывай когда тема глубокая и клиенту полезно "
                        "прочитать подробнее после твоего ответа."
                    ),
                    "items": {"type": "string"},
                    "maxItems": 2,
                },
                "send_reviews_link": {
                    "type": "boolean",
                    "description": (
                        "Приложить ссылку на Yandex Disk с видеоотзывами. "
                        "Используй когда клиент сомневается или просит "
                        "доказательств эффективности."
                    ),
                },
                "send_purchase_links": {
                    "type": "boolean",
                    "description": (
                        "Приложить ссылки Kaspi/офис/WhatsApp для оформления "
                        "заказа. ТОЛЬКО на шаге 5 или когда клиент явно готов купить."
                    ),
                },
                "offer_test": {
                    "type": "boolean",
                    "description": (
                        "Предложить бесплатный тест здоровья (скрининг). "
                        "Используй когда клиент неопределён или не знает "
                        "что сказать."
                    ),
                },
            },
            "required": ["text", "readiness", "intent"],
            "additionalProperties": False,
        },
    },
}


class ClaudeService(IAIService):
    """
    AI service backed by OpenAI (gpt-4o-mini by default).
    Uses low temperature (0.2) for strict adherence to instructions.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    async def generate_response(
        self,
        conversation: Conversation,
        context_docs: List[Document],
        system_prompt: str,
        image_data: Optional[bytes] = None,
        image_media_type: str = "image/jpeg",
    ) -> str:
        # Enrich system prompt with retrieved context.
        # The RAG block is labelled "НАУЧНЫЙ КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ" so the
        # WHATSAPP_INSTRUCTIONS can explicitly reference it and instruct the
        # LLM to quote from it directly.
        enriched_system = system_prompt
        if context_docs:
            sorted_docs = sorted(context_docs, key=lambda d: d.score, reverse=True)
            context_text = "\n\n───\n\n".join(
                f"[Фрагмент {i+1} из {doc.metadata.get('source', 'базы знаний')} — "
                f"релевантность {doc.score:.2f}]\n{doc.content}"
                for i, doc in enumerate(sorted_docs)
            )
            enriched_system = (
                f"{system_prompt}\n\n"
                f"══════════════════════════════════════════════════════════════════\n"
                f"НАУЧНЫЙ КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ\n"
                f"══════════════════════════════════════════════════════════════════\n"
                f"Ниже — реальные фрагменты статей из нашей научной базы "
                f"(Абрахам, Браунштейн, Флечас, материалы центра). "
                f"Ты ОБЯЗАН использовать их в ответе:\n"
                f"— ЦИТИРУЙ ключевые фразы ДОСЛОВНО в кавычках, с указанием "
                f"источника («как пишет Браунштейн в статье X: ...»)\n"
                f"— Приводи КОНКРЕТНЫЕ цифры, годы, имена из этих фрагментов\n"
                f"— Если в фрагментах есть описание механизма — перенеси его "
                f"в свой ответ, не сокращая\n"
                f"— Не заменяй конкретику из базы общими фразами типа "
                f"«это связано с дефицитом йода»\n\n"
                f"{context_text}\n\n"
                f"══════════════════════════════════════════════════════════════════\n"
                f"КОНЕЦ НАУЧНОГО КОНТЕКСТА\n"
                f"══════════════════════════════════════════════════════════════════\n"
            )

        api_messages: list[dict] = [{"role": "system", "content": enriched_system}]

        recent_messages = conversation.get_recent_messages(
            self._settings.max_history_messages
        )
        for msg in recent_messages:
            if msg.role in (Role.USER, Role.ASSISTANT):
                api_messages.append({"role": msg.role.value, "content": msg.content})

        user_messages = [m for m in api_messages if m["role"] == "user"]
        if not user_messages:
            logger.warning("No user messages to send to OpenAI.")
            return ""

        if api_messages[-1]["role"] != "user":
            logger.warning("Last message is not from user.")

        try:
            response = await self._client.chat.completions.create(
                model=self._settings.model_name,
                max_tokens=self._settings.max_response_tokens,
                temperature=self._settings.ai_temperature,
                messages=api_messages,
            )
            if not response.choices:
                logger.warning("OpenAI returned no choices.")
                return ""

            text = (response.choices[0].message.content or "").strip()
            if not text:
                logger.warning("OpenAI returned a blank response.")
            return text

        except openai.RateLimitError as exc:
            logger.error(f"OpenAI rate limit: {exc}")
            return "Извините, сервис временно перегружен. Попробуйте через минуту."
        except openai.APIStatusError as exc:
            logger.error(f"OpenAI API error {exc.status_code}: {exc.message}")
            return "Произошла ошибка при обращении к AI-сервису. Попробуйте ещё раз."
        except openai.APIConnectionError as exc:
            logger.error(f"OpenAI connection error: {exc}")
            return "Нет соединения с сервером. Попробуйте позже."
        except Exception as exc:
            logger.exception(f"Unexpected error in generate_response: {exc}")
            return "Произошла непредвиденная ошибка. Попробуйте позже."

    async def generate_structured_response(
        self,
        conversation: Conversation,
        context_docs: List[Document],
        system_prompt: str,
        image_data: bytes | None = None,
        image_media_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        """Generate a structured response for WhatsApp using forced function calling.

        Returns a dict conforming to WHATSAPP_RESPONSE_TOOL schema:
        {
            text: str (1500-3500 chars),
            readiness: int (0-10),
            intent: str,
            slots: {diagnosis, duration, imaging_done, prior_treatment} (optional),
            advance_to_step: int | None,
            send_videos: [keys] (up to 2),
            attach_articles: [ids] (up to 2),
            send_reviews_link: bool,
            send_purchase_links: bool,
            offer_test: bool,
        }

        Uses tool_choice={"type":"function","function":{"name":"respond_to_client"}}
        to GUARANTEE the LLM emits a structured tool call. Without this, gpt-4o-mini
        ignores tag instructions ~95% of the time in free-form prompts.
        """
        # Enrich system prompt with RAG context
        enriched_system = system_prompt
        if context_docs:
            sorted_docs = sorted(context_docs, key=lambda d: d.score, reverse=True)
            context_text = "\n\n───\n\n".join(
                f"[Фрагмент {i+1} из {doc.metadata.get('source', 'базы знаний')} — "
                f"релевантность {doc.score:.2f}]\n{doc.content}"
                for i, doc in enumerate(sorted_docs)
            )
            enriched_system = (
                f"{system_prompt}\n\n"
                f"══════════════════════════════════════════════════════════════════\n"
                f"НАУЧНЫЙ КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ\n"
                f"══════════════════════════════════════════════════════════════════\n"
                f"Ниже — реальные фрагменты статей из нашей научной базы "
                f"(Абрахам, Браунштейн, Флечас, материалы центра). "
                f"ОБЯЗАТЕЛЬНО используй их в поле 'text' вызова функции "
                f"respond_to_client: цитируй дословно, приводи цифры, имена, годы.\n\n"
                f"{context_text}\n\n"
                f"══════════════════════════════════════════════════════════════════\n"
                f"КОНЕЦ НАУЧНОГО КОНТЕКСТА\n"
                f"══════════════════════════════════════════════════════════════════\n\n"
                f"ВАЖНО: Ты ОБЯЗАН вызвать функцию respond_to_client с заполненными "
                f"параметрами. Не отвечай в свободной форме — только через "
                f"вызов функции. В поле text — твой основной ответ клиенту "
                f"(1500-3500 символов, развёрнуто, с цитатами выше). "
                f"В остальных полях — служебные данные для системы."
            )

        api_messages: list[dict] = [{"role": "system", "content": enriched_system}]

        recent_messages = conversation.get_recent_messages(
            self._settings.max_history_messages
        )
        for i, msg in enumerate(recent_messages):
            if msg.role not in (Role.USER, Role.ASSISTANT):
                continue
            is_last_user = (msg.role == Role.USER and i == len(recent_messages) - 1)
            if is_last_user and image_data:
                # Inject image as vision content block (gpt-4o supports base64 data URIs)
                b64 = base64.standard_b64encode(image_data).decode("utf-8")
                api_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_media_type};base64,{b64}",
                                "detail": "high",
                            },
                        },
                        {
                            "type": "text",
                            "text": msg.content or "Проанализируй этот медицинский документ/снимок.",
                        },
                    ],
                })
            else:
                api_messages.append({"role": msg.role.value, "content": msg.content})

        if not any(m["role"] == "user" for m in api_messages):
            logger.warning("No user messages to send to OpenAI.")
            return {"text": "", "readiness": 0, "intent": "NONE"}

        # Retry on 429 with backoff parsed from OpenAI's "try again in Xs" hint.
        max_retries = 3
        last_429 = None
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._settings.model_name,
                    max_tokens=self._settings.max_response_tokens,
                    temperature=self._settings.ai_temperature,
                    messages=api_messages,
                    tools=[WHATSAPP_RESPONSE_TOOL],
                    tool_choice={
                        "type": "function",
                        "function": {"name": "respond_to_client"},
                    },
                )
                break
            except openai.RateLimitError as exc:
                last_429 = exc
                if attempt >= max_retries:
                    raise
                # Parse "Please try again in 41.088s" hint
                msg = str(exc)
                m = re.search(r"try again in ([\d.]+)\s*s", msg)
                wait = float(m.group(1)) + 2 if m else 30 * (attempt + 1)
                logger.warning(
                    f"OpenAI 429, retry {attempt+1}/{max_retries} in {wait:.0f}s"
                )
                await asyncio.sleep(wait)
        else:
            raise last_429 or RuntimeError("Exhausted retries")
        try:
            if not response.choices:
                logger.warning("OpenAI returned no choices.")
                return {"text": "", "readiness": 0, "intent": "NONE"}

            choice = response.choices[0]
            tool_calls = choice.message.tool_calls or []
            if not tool_calls:
                # Fallback: LLM returned plain text despite forced tool choice
                logger.warning(
                    "Forced tool_choice but no tool_calls — falling back to text"
                )
                return {
                    "text": (choice.message.content or "").strip(),
                    "readiness": 0,
                    "intent": "NONE",
                }

            call = tool_calls[0]
            raw_args = call.function.arguments or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.error(
                    f"Failed to parse tool call JSON (len={len(raw_args)}): {exc}"
                )
                # Attempt salvage: regex-extract the "text" field if JSON
                # was truncated mid-string. Better to return a partial answer
                # than to fail the whole request.
                import re as _re
                text_match = _re.search(
                    r'"text"\s*:\s*"((?:[^"\\]|\\.)*)',
                    raw_args,
                    _re.DOTALL,
                )
                salvaged = ""
                if text_match:
                    salvaged = text_match.group(1).encode().decode("unicode_escape", "ignore")
                if salvaged:
                    logger.warning(f"Salvaged {len(salvaged)} chars from truncated JSON")
                return {
                    "text": salvaged or "Извините, произошла ошибка. Попробуйте ещё раз.",
                    "readiness": 0,
                    "intent": "NONE",
                }

            # Normalise required fields
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

        except openai.RateLimitError as exc:
            logger.error(f"OpenAI rate limit: {exc}")
            return {"text": "Извините, сервис временно перегружен. Попробуйте через минуту.",
                    "readiness": 0, "intent": "NONE"}
        except openai.APIStatusError as exc:
            logger.error(f"OpenAI API error {exc.status_code}: {exc.message}")
            return {"text": "Произошла ошибка при обращении к AI-сервису.",
                    "readiness": 0, "intent": "NONE"}
        except openai.APIConnectionError as exc:
            logger.error(f"OpenAI connection error: {exc}")
            return {"text": "Нет соединения с сервером. Попробуйте позже.",
                    "readiness": 0, "intent": "NONE"}
        except Exception as exc:
            logger.exception(f"Unexpected error in generate_structured_response: {exc}")
            return {"text": "Произошла непредвиденная ошибка.",
                    "readiness": 0, "intent": "NONE"}
