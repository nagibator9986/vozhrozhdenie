"""
FlowController — platform-agnostic business logic extracted from handlers.py.

Does NOT send messages directly. Returns list[OutgoingMessage] which the
transport adapter (TelegramMessenger / WazzupMessenger) sends.
"""
from __future__ import annotations

import re
import time

from loguru import logger

from src.config import Settings
from src.domain.entities import Conversation, FunnelStep
from src.flow.buttons import (
    get_articles_category_buttons,
    get_article_view_buttons,
    get_back_to_menu_buttons,
    get_buy_product_buttons,
    get_capsules_buttons,
    get_funnel_closing_buttons,
    get_funnel_details_buttons,
    get_funnel_education_buttons,
    get_funnel_filter_buttons,
    get_funnel_presentation_buttons,
    get_purchase_buttons,
    get_return_to_funnel_buttons,
    get_reviews_buttons,
    get_screening_result_buttons,
)
from src.flow.constants import (
    AFFIRMATIVE_WORDS,
    FUNNEL_DETAILS_1,
    FUNNEL_DETAILS_2,
    FUNNEL_REJECTED,
    FUNNEL_SCIENCE_TEXT,
    FUNNEL_STEP_1,
    FUNNEL_STEP_2_TEXT,
    FUNNEL_STEP_3_INTRO,
    FUNNEL_STEP_4_TEXT,
    FUNNEL_STEP_5_TEXT,
    HELP_TEXT,
    WHATSAPP_MENU_TEXT,
    WHATSAPP_PERSISTENT_REMINDER,
    WHATSAPP_REMINDER_EVERY_N,
    build_funnel_step_2_videos,
    build_funnel_step_4_videos,
    build_funnel_step_5_contacts,
)
from src.flow.screening import ConversationalScreening
from src.services.articles_service import ArticlesService
from src.services.consultant_service import ConsultantService
from src.services.escalation_service import (
    EscalationReason,  # noqa: F401 — kept for future audit/logging use
    EscalationService,
)
from src.services.telegraph_service import TelegraphService
from src.transport.base import Button, OutgoingMessage


_TEST_OFFER_TTL = 600  # 10 minutes to answer the test offer


# ─────────────────────────────────────────────────────────────────────────────
# Browser-submitted screening result parser
#
# When a WhatsApp user completes the screening quiz on the hosted website,
# the "УЗНАТЬ ГЛАВНУЮ ПРИЧИНУ" button opens wa.me/<bot>?text=<prefilled>
# and the prefilled text arrives in this chat. We detect and parse it here.
#
# Expected format (screening):
#   🔬 Результат скрининга: 12/19
#   Уровень: критический
#   Отмеченные симптомы:
#   • Усталость и отсутствие энергии утром/днём
#   • Депрессия и подавленное настроение
#   ...
#   Хочу узнать главную причину и получить рекомендации.
#
# Expected format (capillary):
#   👁️ Результат теста капилляров: 7/15
#   Уровень: критический
#   Отмеченные признаки:
#   • Видна густая красная сетка сосудов
#   ...
# ─────────────────────────────────────────────────────────────────────────────

_SCREENING_RESULT_RE = re.compile(
    r"🔬\s*Результат\s+скрининга\s*:\s*(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)
_CAPILLARY_RESULT_RE = re.compile(
    r"👁️?\s*Результат\s+теста\s+капилляров\s*:\s*(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)
_LEVEL_MAP_RU = {
    "критический": "high",
    "средний": "medium",
    "начальный": "low",
}


def _parse_browser_screening_result(text: str) -> dict | None:
    """Parse a screening result message sent from the hosted website.

    Returns a dict matching handle_screening_result's expected format,
    or None if the text doesn't match either pattern.
    """
    screening_match = _SCREENING_RESULT_RE.search(text)
    capillary_match = _CAPILLARY_RESULT_RE.search(text)

    if not screening_match and not capillary_match:
        return None

    is_capillary = capillary_match is not None
    match = capillary_match if is_capillary else screening_match
    try:
        score = int(match.group(1))
        total = int(match.group(2))
    except (ValueError, AttributeError):
        return None

    # Extract level
    level = "unknown"
    for ru_word, code in _LEVEL_MAP_RU.items():
        if ru_word in text.lower():
            level = code
            break
    # Fallback: compute level from score if not specified in text
    if level == "unknown":
        if is_capillary:
            level = "high" if score >= 7 else "medium" if score >= 3 else "low"
        else:
            level = "high" if score >= 9 else "medium" if score >= 4 else "low"

    # Extract bulleted symptoms (lines starting with •)
    symptoms: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("•"):
            sym = stripped.lstrip("•").strip()
            if sym:
                symptoms.append(sym)

    result_key = "signs" if is_capillary else "symptoms"
    return {
        "type": "capillary_result" if is_capillary else "screening_result",
        result_key: symptoms,
        "score": score,
        "total": total,
        "level": level,
    }


class FlowController:
    """Platform-agnostic flow engine. Returns OutgoingMessage lists."""

    def __init__(
        self,
        consultant: ConsultantService,
        settings: Settings,
        articles: ArticlesService,
        telegraph: TelegraphService | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self._consultant = consultant
        self._settings = settings
        self._articles = articles
        self._telegraph = telegraph
        self._escalation = escalation or EscalationService()
        self._screening = ConversationalScreening()
        # In-memory state: users who were offered the test and haven't answered yet
        self._pending_test_offer: dict[str, float] = {}

    @property
    def articles(self) -> "ArticlesService":
        """Public accessor for the articles registry (used by the webhook to
        resolve article IDs without reaching into a private attribute)."""
        return self._articles

    @staticmethod
    def _detect_platform(user_id: str) -> str:
        """Detect platform from user_id prefix (tg: or wa:)."""
        if user_id.startswith("wa:"):
            return "whatsapp"
        return "telegram"

    def _cleanup_test_offers(self) -> None:
        """Remove expired test offers."""
        now = time.time()
        expired = [uid for uid, ts in self._pending_test_offer.items() if now - ts > _TEST_OFFER_TTL]
        for uid in expired:
            self._pending_test_offer.pop(uid, None)

    # ── Command handlers ─────────────────────────────────────────────

    async def handle_start(self, user_id: str, platform: str = "telegram") -> list[OutgoingMessage]:
        conversation = await self._consultant.get_or_create_conversation(user_id)
        conversation.funnel_step = FunnelStep.STEP_1_FILTER
        conversation.messages = []
        await self._consultant.save_conversation(conversation)

        messages: list[OutgoingMessage] = []

        if platform == "whatsapp":
            # Warm human-tone greeting for WhatsApp — no fake name, just the centre
            screening_url = self._settings.screening_url or ""
            test_offer = ""
            if screening_url and screening_url.startswith("http") and "localhost" not in screening_url:
                test_offer = (
                    f"\n\nЕсли вы не уверены в диагнозе или не знаете с чего "
                    f"начать — пройдите наш бесплатный 1-минутный тест, "
                    f"он выявит возможный дефицит йода и системы которые "
                    f"в первую очередь нуждаются в восстановлении:\n{screening_url}"
                )
            messages.append(OutgoingMessage(
                text=(
                    "Здравствуйте! 🌿\n\n"
                    "Вас приветствует консультационный центр почётного академика "
                    "Мурата Турлубекова. Уже 25 лет мы помогаем восстанавливать "
                    "здоровье щитовидной железы, молочных желёз, простаты, "
                    "яичников и других йод-зависимых органов — без операций, "
                    "через протокол йодотерапии академика.\n\n"
                    "За эти годы более 500 000 клиентов — и женщин, и мужчин — "
                    "прошли наш курс. Расскажите, что вас беспокоит — "
                    "разберёмся вместе."
                    f"{test_offer}"
                ),
                parse_mode=None,
            ))
        else:
            messages.append(OutgoingMessage(text=FUNNEL_STEP_1, buttons=get_funnel_filter_buttons()))

        return messages

    async def handle_help(self) -> list[OutgoingMessage]:
        return [OutgoingMessage(text=HELP_TEXT)]

    async def handle_articles_command(self) -> list[OutgoingMessage]:
        categories = self._articles.all_categories()
        if not categories:
            return [OutgoingMessage(text="📚 Библиотека статей пока не загружена.", parse_mode=None)]
        return [
            OutgoingMessage(
                text="📚 *Научная библиотека*\n\nВыберите категорию:",
                buttons=get_articles_category_buttons(categories),
            )
        ]

    # ── Callback handler (routes by callback_data) ───────────────────

    async def handle_callback(self, user_id: str, callback_data: str) -> list[OutgoingMessage]:
        # Funnel callbacks
        if callback_data == "funnel:accept":
            return await self._cb_funnel_accept(user_id)
        elif callback_data == "funnel:reject":
            return await self._cb_funnel_reject(user_id)
        elif callback_data == "funnel:details":
            return await self._cb_funnel_details(user_id)
        elif callback_data == "funnel:question":
            return self._cb_funnel_question()
        elif callback_data == "funnel:to_presentation":
            return await self._cb_funnel_to_presentation(user_id)
        elif callback_data == "funnel:science":
            return self._cb_funnel_science()
        elif callback_data == "funnel:buy_now":
            return await self._cb_funnel_buy_now(user_id)
        elif callback_data == "funnel:objection":
            return await self._cb_funnel_objection(user_id)
        elif callback_data == "funnel:to_qualification":
            return await self._cb_funnel_to_qualification(user_id)

        # Test offer callbacks
        elif callback_data == "test:yes":
            return await self._handle_test_offer_answer(user_id, "да")
        elif callback_data == "test:no":
            return await self._handle_test_offer_answer(user_id, "нет")

        # Menu callbacks
        elif callback_data == "menu_buy":
            return self._cb_buy()
        elif callback_data == "buy_powder":
            return self._cb_buy_powder()
        elif callback_data == "buy_capsules":
            return self._cb_buy_capsules()
        elif callback_data == "menu_reviews":
            return self._cb_reviews()
        elif callback_data == "menu_articles":
            return await self.handle_articles_command()
        elif callback_data == "menu_back":
            return [OutgoingMessage(text="Чем ещё могу помочь? Напишите вопрос или выберите действие.", parse_mode=None)]

        # Article callbacks
        elif callback_data.startswith("articles_cat:"):
            return self._cb_articles_category(callback_data)
        elif callback_data.startswith("articles_page:"):
            return self._cb_articles_page(callback_data)
        elif callback_data == "articles_noop":
            return []
        elif callback_data.startswith("article_send:"):
            return self._cb_article_send(callback_data)
        elif callback_data.startswith("article_file:"):
            return self._cb_article_file(callback_data)

        logger.warning(f"Unknown callback_data: {callback_data}")
        return []

    # ── Text handler (the big one) ───────────────────────────────────

    async def handle_text(
        self, user_id: str, text: str, platform: str = "telegram",
        image_data: bytes | None = None,
        image_media_type: str = "image/jpeg",
    ) -> list[OutgoingMessage]:
        text = text.strip()
        if not text:
            return []

        logger.info(f"FlowController text from {user_id}: {text[:80]!r}")
        conversation = await self._consultant.get_or_create_conversation(user_id)
        current_step = conversation.funnel_step

        # Cleanup expired test offers (memory hygiene for long-running bot)
        self._cleanup_test_offers()

        # ── Check for active screening session ──
        if self._screening.has_active_session(user_id):
            return await self._handle_screening_answer(user_id, text)

        # ── Check for pending test offer (WhatsApp flow) ──
        # If user gives ambiguous answer, _handle_test_offer_answer returns []
        # and we continue with normal flow below.
        if user_id in self._pending_test_offer:
            test_response = await self._handle_test_offer_answer(user_id, text)
            if test_response:
                return test_response
            # Ambiguous — continue with normal processing but keep pending

        # ── NOT_STARTED → auto-start funnel ──
        if current_step == FunnelStep.NOT_STARTED:
            return await self.handle_start(user_id, platform=platform)

        # ── REJECTED → offer restart ──
        if current_step == FunnelStep.REJECTED:
            restart_hint = 'напишите "заново"' if platform == "whatsapp" else "напишите /start"
            return [OutgoingMessage(
                text=f"Если передумали — {restart_hint} чтобы начать заново.",
                parse_mode=None,
            )]

        # ── WhatsApp numbered menu commands ──
        if platform == "whatsapp":
            menu_result = await self._handle_whatsapp_menu(user_id, text)
            if menu_result is not None:
                return menu_result

        # ── STEP_3 (Qualification) → text IS the diagnosis ──
        # For WhatsApp: LLM handles qualification itself via process_message_rich
        # using the funnel-aware system prompt (WHATSAPP_INSTRUCTIONS). The LLM
        # asks one slot-question per turn, extracts SLOTS tags, and advances
        # via [ADVANCE:4] when enough facts are collected. Skipping the legacy
        # _handle_qualification preserves attachments (SEND/ATTACH) and
        # [ADVANCE:N] progression.
        if current_step == FunnelStep.STEP_3_QUALIFICATION and platform != "whatsapp":
            return await self._handle_qualification(user_id, text)

        # ── Detect affirmative replies ──
        # Telegram only — it routes "да" to hardcoded callback handlers that
        # send Instagram URLs. On WhatsApp we let the LLM interpret "да" in
        # context (it knows the current funnel step) and reply via process_
        # message_rich, which is smarter and attaches proper videos/articles.
        if platform != "whatsapp" and text.lower().strip() in AFFIRMATIVE_WORDS:
            result = await self._handle_affirmative(user_id, current_step)
            if result is not None:
                return result

        # ── ANY OTHER STEP → AI answers ──
        try:
            if platform == "whatsapp":
                # Rich result with attachments + funnel advancement
                result = await self._consultant.process_message_rich(
                    user_id, text, platform=platform,
                    image_data=image_data, image_media_type=image_media_type,
                )
                messages: list[OutgoingMessage] = [
                    OutgoingMessage(text=result.text, parse_mode=None)
                ]
                # Append catalogue-resolved videos/articles AFTER the text
                messages.extend(result.attachments)
                return messages

            # Telegram: legacy string response + funnel nav buttons
            response = await self._consultant.process_message(user_id, text, platform=platform)
            messages = [OutgoingMessage(text=response)]
            messages.append(OutgoingMessage(
                text="Хотите продолжить?",
                buttons=get_return_to_funnel_buttons(current_step.value),
                parse_mode=None,
            ))
            return messages
        except Exception as exc:
            logger.exception(f"Error processing message for {user_id}: {exc}")
            return [OutgoingMessage(
                text="❌ Произошла ошибка. Попробуйте ещё раз или напишите /start.",
                parse_mode=None,
            )]

    async def _maybe_append_reminder(self, user_id: str, response: str) -> str:
        """Append persistent reminder every N-th AI response on WhatsApp.

        Uses a counter stored in qualification_facts under key "_bot_replies"
        to avoid needing a new DB column.
        """
        conv = await self._consultant.get_or_create_conversation(user_id)
        counter = int(conv.qualification_facts.get("_bot_replies", 0)) + 1
        conv.qualification_facts["_bot_replies"] = str(counter)
        await self._consultant.save_conversation(conv)

        # Append reminder on every Nth message (but not on the very first)
        if counter > 1 and counter % WHATSAPP_REMINDER_EVERY_N == 0:
            return response + WHATSAPP_PERSISTENT_REMINDER
        return response

    # ── WhatsApp-specific entry point ────────────────────────────────

    async def handle_whatsapp_message(
        self, user_id: str, chat_id: str, text: str,
        has_media: bool = False,
        image_data: bytes | None = None,
        image_media_type: str = "image/jpeg",
        document_text: str | None = None,
    ) -> list[OutgoingMessage]:
        """Entry point for WhatsApp messages from webhook.

        Args:
            user_id: "wa:<phone>"
            chat_id: phone number
            text: transcribed/typed text (may be empty for media-only messages)
            has_media: True if user sent an unsupported media type
            image_data: raw bytes of an image for vision analysis
            image_media_type: MIME type of the image
            document_text: extracted text from a PDF/DOCX document
        """
        self._cleanup_test_offers()

        conversation = await self._consultant.get_or_create_conversation(user_id)

        # ── PRIORITY 0: Operator mode — bot stays silent ──
        if self._escalation.is_active(conversation):
            logger.info(f"Operator mode active for {user_id} — bot silent")
            return []

        # ── PRIORITY 1a: Image received — pass to AI for vision analysis ──
        if image_data:
            analysis_text = text or "Пожалуйста, проанализируй этот медицинский снимок/документ, связав все находки с дефицитом йода."
            return await self.handle_text(
                user_id, analysis_text, platform="whatsapp",
                image_data=image_data, image_media_type=image_media_type,
            )

        # ── PRIORITY 1b: Document text extracted — inject into message ──
        if document_text:
            injected = (
                f"[МЕДИЦИНСКИЙ ДОКУМЕНТ ПОЛУЧЕН]\n\n"
                f"{document_text}\n\n"
                f"{text or 'Проанализируй этот документ и свяжи все находки с дефицитом йода.'}"
            )
            return await self.handle_text(user_id, injected, platform="whatsapp")

        # ── PRIORITY 1c: Unsupported media — ask to describe ──
        if has_media:
            return [OutgoingMessage(
                text=(
                    "Получил ваше вложение, но этот тип файла мне пока не доступен для анализа.\n\n"
                    "Пришлите фото снимка (МРТ, УЗИ, маммография) — я сделаю полный разбор. "
                    "Или напишите текстом что именно нашли в заключении — "
                    "я разберу каждый пункт и свяжу с причиной."
                ),
                parse_mode=None,
            )]

        # Note: serious diagnosis and human-request handling are now inside
        # the LLM itself (see WHATSAPP_INSTRUCTIONS). The bot is the only
        # consultant — it does NOT escalate. The old "serious_diagnosis" and
        # "human_request" detectors are retained in escalation_service.py for
        # logging/audit only, but are not branching the flow anymore.

        # ── PRIORITY 3: Browser-submitted screening result ──
        # User completed quiz on the hosted website and came back via wa.me link
        # with pre-filled result text. Parse it and run the same AI analysis
        # as for Telegram WebApp results.
        if text:
            parsed_result = _parse_browser_screening_result(text)
            if parsed_result:
                logger.info(
                    f"Browser screening result from {user_id}: "
                    f"{parsed_result['score']}/{parsed_result['total']}"
                )
                # Clear pending test offer since user actually completed the test
                self._pending_test_offer.pop(user_id, None)
                return await self.handle_screening_result(user_id, parsed_result)

        # ── PRIORITY 4: Active conversational screening session ──
        if self._screening.has_active_session(user_id):
            return await self._handle_screening_answer(user_id, text)

        # ── PRIORITY 5: Callback button press ──
        # Button presses take priority over pending test offer — they're unambiguous.
        callback = self._match_whatsapp_button(text)
        if callback:
            if user_id in self._pending_test_offer:
                self._pending_test_offer.pop(user_id, None)
            return await self.handle_callback(user_id, callback)

        # ── PRIORITY 6: Pending test offer (awaiting yes/no) ──
        if user_id in self._pending_test_offer:
            test_response = await self._handle_test_offer_answer(user_id, text)
            if test_response:
                return test_response

        # ── Default: normal text handling ──
        return await self.handle_text(user_id, text, platform="whatsapp")

    # ── Screening result handler ─────────────────────────────────────

    async def handle_screening_result(self, user_id: str, result: dict) -> list[OutgoingMessage]:
        """Process screening quiz result (from WebApp or conversational)."""
        result_type = result.get("type", "")
        symptoms = result.get("symptoms") or result.get("signs") or []
        score = result.get("score", 0)
        total = result.get("total", 19)
        level = result.get("level", "unknown")
        level_text = (
            "критический" if level == "high"
            else "средний" if level == "medium"
            else "начальный"
        )

        is_capillary = result_type == "capillary_result"
        test_name = "капилляров" if is_capillary else "здоровья"
        test_emoji = "👁️" if is_capillary else "🔬"

        logger.info(f"{result_type} for {user_id}: {score}/{total} ({level})")

        symptom_list = "\n".join(f"• {s}" for s in symptoms)

        if is_capillary:
            analysis_prompt = (
                f"Клиент прошёл тест капилляров (осмотр белка глаз + слух + сосуды). "
                f"Результат: {score} из {total} признаков засорения капилляров.\n"
                f"Уровень: {level_text}.\n\n"
                f"Отмеченные признаки:\n{symptom_list}\n\n"
                f"ЗАДАЧА: Дай персональный анализ состояния сосудов. Объясни, что "
                f"засорение капилляров галогенами (фтор, хлор, бром) — это ПРИЧИНА "
                f"всех отмеченных симптомов. Белок глаз — зеркало сосудов всего тела. "
                f"Покажи, что йод промывает капилляры и восстанавливает "
                f"микроциркуляцию. Упомяни пример академика Турлубекова (65 лет, "
                f"выглядит на 40, МРТ органов 25-летнего)."
            )
        else:
            analysis_prompt = (
                f"Клиент прошёл скрининг здоровья. "
                f"Результат: {score} из {total} маркеров клеточного голодания.\n"
                f"Уровень: {level_text}.\n\n"
                f"Отмеченные симптомы:\n{symptom_list}\n\n"
                f"ЗАДАЧА: Дай персональный анализ. Объясни, почему ВСЕ эти симптомы "
                f"связаны с одной причиной — дефицитом йода и замещением его токсичными "
                f"галогенами (фтор, хлор, бром) в щитовидной железе. "
                f"Покажи связь каждого симптома с щитовидной. "
                f"Заверши тем, что есть решение — высокодозная йодотерапия."
            )

        messages: list[OutgoingMessage] = []
        platform = self._detect_platform(user_id)
        try:
            response = await self._consultant.process_message(user_id, analysis_prompt, platform=platform)
            header = f"{test_emoji} *Результат теста {test_name}: {score}/{total}*\n\n"
            messages.append(OutgoingMessage(text=header + response))
        except Exception as exc:
            logger.error(f"{result_type} analysis error for {user_id}: {exc}")
            messages.append(OutgoingMessage(
                text=f"{test_emoji} *Результат теста: {score}/{total}*\n\n"
                     "К сожалению, не удалось провести анализ. Расскажите о своих "
                     "симптомах текстом — я помогу разобраться.",
            ))

        # Advance funnel
        conversation = await self._consultant.get_or_create_conversation(user_id)
        if conversation.funnel_step.value < FunnelStep.STEP_3_QUALIFICATION.value:
            conversation.funnel_step = FunnelStep.STEP_3_QUALIFICATION
            await self._consultant.save_conversation(conversation)

        messages.append(OutgoingMessage(
            text="Хотите узнать подробнее о причине и решении?",
            buttons=get_screening_result_buttons(),
            parse_mode=None,
        ))

        return messages

    # ── Private: funnel callback implementations ─────────────────────

    async def _cb_funnel_accept(self, user_id: str) -> list[OutgoingMessage]:
        conversation = await self._consultant.get_or_create_conversation(user_id)
        conversation.funnel_step = FunnelStep.STEP_2_EDUCATION
        await self._consultant.save_conversation(conversation)

        platform = self._detect_platform(user_id)

        messages = [
            OutgoingMessage(text=FUNNEL_STEP_2_TEXT),
            OutgoingMessage(
                text=build_funnel_step_2_videos(self._settings),
                buttons=get_funnel_education_buttons(),
                parse_mode=None,
            ),
        ]

        if self._settings.screening_url:
            if platform == "whatsapp":
                # Explicit test offer for WhatsApp — awaits yes/no answer
                self._pending_test_offer[user_id] = time.time()
                self._cleanup_test_offers()
                messages.append(OutgoingMessage(
                    text=(
                        "💡 А вы знаете свой уровень дефицита йода?\n\n"
                        "За 2 минуты можно пройти короткий тест на 19 признаков "
                        "клеточного голодания и сразу увидеть результат.\n\n"
                        "Хотите пройти?"
                    ),
                    buttons=[
                        Button(text="Да, пройти тест", callback_data="test:yes"),
                        Button(text="Нет, продолжим", callback_data="test:no"),
                    ],
                    parse_mode=None,
                ))
            else:
                # Telegram: keep the WebApp-friendly message (the actual WebApp button
                # is sent by the Telegram handler directly)
                messages.append(OutgoingMessage(
                    text="🔬 *Проверьте себя за 2 минуты* — узнайте, "
                         "затронул ли дефицит йода ваш организм:",
                ))

        return messages

    async def _handle_test_offer_answer(self, user_id: str, text: str) -> list[OutgoingMessage]:
        """Handle yes/no answer after bot offered the test (WhatsApp flow).

        Strict matching: accept only clear affirmative phrases.
        Ambiguous responses ("а сколько стоит тест?") fall through to normal flow.
        """
        lower = text.lower().strip()

        # Clear affirmative phrases (WhatsApp button texts + natural "yes")
        affirmative_phrases = {
            "да", "ок", "окей", "давай", "давайте", "конечно",
            "yes", "хочу пройти", "хочу тест", "пройти тест",
            "да, пройти тест", "да пройти тест", "готов", "готова",
            "пройду", "буду проходить",
        }
        # Clear negative phrases
        negative_phrases = {
            "нет", "не", "не надо", "не нужно", "не хочу", "no",
            "нет, продолжим", "нет продолжим", "продолжим",
            "продолжить", "потом", "позже", "не сейчас", "пропустить",
        }

        # Exact or startswith match (more reliable than substring)
        is_affirmative = (
            lower in affirmative_phrases
            or any(lower.startswith(p + " ") or lower.startswith(p + ",") for p in affirmative_phrases)
        )
        is_negative = (
            lower in negative_phrases
            or any(lower.startswith(p + " ") or lower.startswith(p + ",") for p in negative_phrases)
        )

        # Ambiguous response (question about the test, etc.) — keep pending, treat as normal message
        if not is_affirmative and not is_negative:
            # Don't pop — user may answer yes/no on next turn
            return []

        # Clear answer — remove pending state
        self._pending_test_offer.pop(user_id, None)

        if is_affirmative:
            url = self._settings.screening_url or ""
            if not url:
                return [OutgoingMessage(
                    text=(
                        "К сожалению, тест временно недоступен.\n\n"
                        "Расскажите свои симптомы прямо в чате — я проведу анализ "
                        "и дам персональные рекомендации."
                    ),
                    parse_mode=None,
                )]
            return [OutgoingMessage(
                text=(
                    f"Отлично! 🔬\n\n"
                    f"Вот ссылка на тест (займёт 2 минуты):\n{url}\n\n"
                    f"После прохождения напишите мне результат или ваши главные "
                    f"симптомы — я дам персональный анализ и подскажу протокол."
                ),
                parse_mode=None,
            )]

        # Negative → continue funnel with details content
        return await self._cb_funnel_details(user_id)

    async def _cb_funnel_reject(self, user_id: str) -> list[OutgoingMessage]:
        conversation = await self._consultant.get_or_create_conversation(user_id)
        conversation.funnel_step = FunnelStep.REJECTED
        await self._consultant.save_conversation(conversation)
        return [OutgoingMessage(text=FUNNEL_REJECTED)]

    async def _cb_funnel_details(self, user_id: str) -> list[OutgoingMessage]:
        return [
            OutgoingMessage(text=FUNNEL_DETAILS_1),
            OutgoingMessage(text=FUNNEL_DETAILS_2, buttons=get_funnel_details_buttons()),
        ]

    def _cb_funnel_question(self) -> list[OutgoingMessage]:
        return [OutgoingMessage(
            text="💬 Напишите ваш вопрос — я отвечу и мы продолжим!\n\n"
                 "Вы можете спросить о:\n"
                 "• Симптомах и диагнозах\n"
                 "• Дозировках и кофакторах\n"
                 "• Детокс-симптомах (кризис очищения)\n"
                 "• Научных доказательствах\n"
                 "• Стоимости и заказе",
        )]

    async def _cb_funnel_to_presentation(self, user_id: str) -> list[OutgoingMessage]:
        conversation = await self._consultant.get_or_create_conversation(user_id)
        if conversation.funnel_step.value < FunnelStep.STEP_4_PRESENTATION.value:
            conversation.funnel_step = FunnelStep.STEP_4_PRESENTATION
            await self._consultant.save_conversation(conversation)

        return [
            OutgoingMessage(text=FUNNEL_STEP_4_TEXT),
            OutgoingMessage(
                text=build_funnel_step_4_videos(self._settings),
                buttons=get_funnel_presentation_buttons(),
                parse_mode=None,
            ),
        ]

    def _cb_funnel_science(self) -> list[OutgoingMessage]:
        return [OutgoingMessage(
            text=FUNNEL_SCIENCE_TEXT,
            buttons=get_funnel_presentation_buttons(),
        )]

    async def _cb_funnel_buy_now(self, user_id: str) -> list[OutgoingMessage]:
        conversation = await self._consultant.get_or_create_conversation(user_id)
        conversation.funnel_step = FunnelStep.STEP_5_CLOSING
        await self._consultant.save_conversation(conversation)

        return [
            OutgoingMessage(text=FUNNEL_STEP_5_TEXT),
            OutgoingMessage(
                text=build_funnel_step_5_contacts(self._settings),
                buttons=get_funnel_closing_buttons(self._settings),
                parse_mode=None,
            ),
        ]

    async def _cb_funnel_objection(self, user_id: str) -> list[OutgoingMessage]:
        try:
            objection_text = "У меня есть сомнения перед покупкой. Помогите разобраться."
            response = await self._consultant.process_message(user_id, objection_text)
            return [OutgoingMessage(text=response)]
        except Exception as exc:
            logger.error(f"funnel:objection error for {user_id}: {exc}")
            return [OutgoingMessage(text="❌ Произошла ошибка. Напишите ваш вопрос.", parse_mode=None)]

    async def _cb_funnel_to_qualification(self, user_id: str) -> list[OutgoingMessage]:
        conversation = await self._consultant.get_or_create_conversation(user_id)
        conversation.funnel_step = FunnelStep.STEP_3_QUALIFICATION
        await self._consultant.save_conversation(conversation)
        return [OutgoingMessage(text=FUNNEL_STEP_3_INTRO)]

    # ── Private: menu callback implementations ───────────────────────

    def _cb_buy(self) -> list[OutgoingMessage]:
        return [OutgoingMessage(
            text="🛒 *Выберите форму Бальзам Возрождение Plus:*",
            buttons=get_buy_product_buttons(),
        )]

    def _cb_buy_powder(self) -> list[OutgoingMessage]:
        s = self._settings
        return [OutgoingMessage(
            text=(
                "🧴 *Бальзам Возрождение Plus (порошок)*\n\n"
                "• 500 мг йода в 1 чайной ложке\n"
                "• Курс: 3-6 месяцев\n"
                "• Стоимость: *750 000 тг* (3-мес. курс)\n"
                "• Результат быстрее, чем капсулы\n\n"
                "Для сравнения: операция — от 1 000 000 тг за ОДИН орган.\n\n"
                f"🏢 Офис: {s.office_address}"
            ),
            buttons=get_purchase_buttons(s),
        )]

    def _cb_buy_capsules(self) -> list[OutgoingMessage]:
        return [OutgoingMessage(
            text=(
                "💊 *Бальзам Возрождение Plus (капсулы)*\n\n"
                "• 25 мг йода в 1 капсуле (принимать 4 шт/день)\n"
                "• Курс: 12-16 месяцев\n"
                "• Упаковки: 30 / 60 / *90 капсул* (выгоднее)\n\n"
                "Заказать через Kaspi — рассрочка и кредит!"
            ),
            buttons=get_capsules_buttons(self._settings.kaspi_capsules_url),
        )]

    def _cb_reviews(self) -> list[OutgoingMessage]:
        return [OutgoingMessage(
            text="📹 *Видеоотзывы клиентов Бальзама Возрождение Plus*\n\n"
                 "Реальные истории выздоровления:",
            buttons=get_reviews_buttons(self._settings.yandex_disk_reviews_url),
        )]

    # ── Private: article callbacks ───────────────────────────────────

    def _cb_articles_category(self, callback_data: str) -> list[OutgoingMessage]:
        category = callback_data.split(":", 1)[1]
        cat_articles, total_pages = self._articles.list_by_category_paged(category, 0)
        if not cat_articles:
            return [OutgoingMessage(text="В этой категории статей нет.", parse_mode=None)]

        cat_label = dict(self._articles.all_categories()).get(category, category)
        page_info = f" — стр. 1/{total_pages}" if total_pages > 1 else ""

        buttons = []
        for art in cat_articles:
            lang_flag = " 🇬🇧" if art.language == "en" else ""
            buttons.append(Button(text=f"📄 {art.title}{lang_flag}", callback_data=f"article_send:{art.id}"))

        if total_pages > 1:
            buttons.append(Button(text=f"1/{total_pages}", callback_data="articles_noop"))
            buttons.append(Button(text="▶️", callback_data=f"articles_page:{category}:1"))

        buttons.append(Button(text="🔙 К категориям", callback_data="menu_articles"))

        return [OutgoingMessage(
            text=f"📁 *{cat_label}*{page_info}\n\nВыберите статью:",
            buttons=buttons,
        )]

    def _cb_articles_page(self, callback_data: str) -> list[OutgoingMessage]:
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            return []
        _, category, page_str = parts
        try:
            page = int(page_str)
        except ValueError:
            return []

        cat_articles, total_pages = self._articles.list_by_category_paged(category, page)
        if not cat_articles:
            return []

        cat_label = dict(self._articles.all_categories()).get(category, category)
        page_info = f" — стр. {page + 1}/{total_pages}" if total_pages > 1 else ""

        buttons = []
        for art in cat_articles:
            lang_flag = " 🇬🇧" if art.language == "en" else ""
            buttons.append(Button(text=f"📄 {art.title}{lang_flag}", callback_data=f"article_send:{art.id}"))

        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(Button(text="◀️", callback_data=f"articles_page:{category}:{page - 1}"))
            nav.append(Button(text=f"{page + 1}/{total_pages}", callback_data="articles_noop"))
            if page < total_pages - 1:
                nav.append(Button(text="▶️", callback_data=f"articles_page:{category}:{page + 1}"))
            buttons.extend(nav)

        buttons.append(Button(text="🔙 К категориям", callback_data="menu_articles"))

        return [OutgoingMessage(
            text=f"📁 *{cat_label}*{page_info}\n\nВыберите статью:",
            buttons=buttons,
        )]

    def _cb_article_send(self, callback_data: str) -> list[OutgoingMessage]:
        article_id = callback_data.split(":", 1)[1]
        article = self._articles.get_by_id(article_id)
        if not article:
            return [OutgoingMessage(text="❌ Статья не найдена.", parse_mode=None)]

        lang_note = " 🇬🇧" if article.language == "en" else ""
        telegraph_url = None
        if self._telegraph and self._telegraph.is_available():
            telegraph_url = self._telegraph.get_url(article)
            if not telegraph_url:
                telegraph_url = self._telegraph.publish(article)

        return [OutgoingMessage(
            text=f"📄 *{article.title}*{lang_note}\n\n_{article.description}_\n\nВыберите способ просмотра:",
            buttons=get_article_view_buttons(article.id, telegraph_url),
        )]

    def _cb_article_file(self, callback_data: str) -> list[OutgoingMessage]:
        """Return article file info. Actual file sending is handled by transport layer."""
        article_id = callback_data.split(":", 1)[1]
        article = self._articles.get_by_id(article_id)
        if not article:
            return [OutgoingMessage(text="❌ Статья не найдена.", parse_mode=None)]

        # Telegraph URL is the best option for WhatsApp (no file upload needed)
        telegraph_url = None
        if self._telegraph and self._telegraph.is_available():
            telegraph_url = self._telegraph.get_url(article)
            if not telegraph_url:
                telegraph_url = self._telegraph.publish(article)

        if telegraph_url:
            lang_note = " 🇬🇧" if article.language == "en" else ""
            return [
                OutgoingMessage(
                    text=f"📄 *{article.title}*{lang_note}\n\n_{article.description}_\n\n"
                         f"📖 Читать: {telegraph_url}",
                ),
                OutgoingMessage(
                    text="Хотите посмотреть другие статьи?",
                    buttons=get_back_to_menu_buttons(),
                    parse_mode=None,
                ),
            ]

        # Fallback: send article text preview
        text_content = article.read_text()
        if text_content:
            preview = text_content[:3000] + ("..." if len(text_content) > 3000 else "")
            return [
                OutgoingMessage(text=f"📄 *{article.title}*\n\n{preview}"),
                OutgoingMessage(
                    text="Хотите посмотреть другие статьи?",
                    buttons=get_back_to_menu_buttons(),
                    parse_mode=None,
                ),
            ]

        return [OutgoingMessage(text="❌ Не удалось загрузить файл статьи.", parse_mode=None)]

    # ── Private: qualification handling ──────────────────────────────

    # Required qualification slots in priority order
    _QUAL_SLOTS = ("diagnosis", "duration", "imaging_done", "prior_treatment")
    _MAX_QUAL_TURNS = 5  # Force-advance to presentation after this many turns

    def _qualification_complete(self, conversation: Conversation) -> bool:
        """Check if at least 3 of 4 required qualification slots are filled.

        Only counts real slots (listed in _QUAL_SLOTS), ignoring internal
        counters like _turns and _bot_replies.
        """
        facts = conversation.qualification_facts
        filled = sum(
            1 for slot in self._QUAL_SLOTS
            if slot in facts and facts[slot] and not slot.startswith("_")
        )
        return filled >= 3

    async def _handle_qualification(self, user_id: str, text: str) -> list[OutgoingMessage]:
        """Multi-turn qualification handler with slot filling (WhatsApp-aware).

        - Telegram: one-shot qualification (legacy behavior)
        - WhatsApp: multi-turn, one question at a time, max 5 turns
        """
        platform = self._detect_platform(user_id)

        # Track qualification turn count — persist BEFORE calling process_message
        # so that process_message can read it. Otherwise process_message re-reads
        # conversation from DB and the counter update is lost.
        conversation = await self._consultant.get_or_create_conversation(user_id)
        qual_turns = int(conversation.qualification_facts.get("_turns", 0)) + 1
        conversation.qualification_facts["_turns"] = str(qual_turns)
        await self._consultant.save_conversation(conversation)

        try:
            if platform == "whatsapp":
                # WhatsApp: rely on LLM to ask next question via SLOTS extraction
                # process_message will load the current state and add user message
                response = await self._consultant.process_message(
                    user_id, text, platform="whatsapp"
                )
            else:
                # Telegram: legacy qualification prompt
                qual_prompt = (
                    f"Клиент описывает свою ситуацию: {text}\n\n"
                    "Дай персональный ответ на основе его диагноза. "
                    "Объясни, как бальзам поможет конкретно в его случае. "
                    "Упомяни дозировки и кофакторы."
                )
                response = await self._consultant.process_message(user_id, qual_prompt)

            messages: list[OutgoingMessage] = [OutgoingMessage(text=response)]
        except Exception as exc:
            logger.error(f"Qualification AI error for {user_id}: {exc}")
            return [OutgoingMessage(text="❌ Произошла ошибка. Попробуйте ещё раз.", parse_mode=None)]

        # Reload conversation AFTER process_message — it has updated qualification_facts
        # with LLM-extracted slots (diagnosis, duration, etc.)
        conversation = await self._consultant.get_or_create_conversation(user_id)

        # Read the turn counter from the reloaded state (most up-to-date)
        qual_turns_final = int(conversation.qualification_facts.get("_turns", qual_turns))

        # Advance to presentation if:
        # 1. Enough slots filled (3 of 4) OR
        # 2. Exceeded max turns (force advance) OR
        # 3. Telegram platform (legacy: always advance after one turn)
        should_advance = (
            platform == "telegram"
            or self._qualification_complete(conversation)
            or qual_turns_final >= self._MAX_QUAL_TURNS
        )

        if should_advance:
            conversation.funnel_step = FunnelStep.STEP_4_PRESENTATION
            # Clean up internal counters so they don't pollute qualification_facts
            conversation.qualification_facts.pop("_turns", None)
            conversation.qualification_facts.pop("_bot_replies", None)
            await self._consultant.save_conversation(conversation)

            # Telegram: show inline buttons. WhatsApp: LLM handles next step naturally.
            if platform == "telegram":
                messages.append(OutgoingMessage(
                    text="Хотите узнать подробности о продукте и как его принимать?",
                    buttons=get_funnel_presentation_buttons(),
                    parse_mode=None,
                ))
        else:
            # Save intermediate qualification state (includes updated _turns from LLM call)
            await self._consultant.save_conversation(conversation)

        return messages

    # ── Private: affirmative auto-advance ────────────────────────────

    async def _handle_affirmative(self, user_id: str, step: FunnelStep) -> list[OutgoingMessage] | None:
        if step == FunnelStep.STEP_1_FILTER:
            return await self._cb_funnel_accept(user_id)
        elif step == FunnelStep.STEP_2_EDUCATION:
            return await self._cb_funnel_details(user_id)
        elif step in (FunnelStep.STEP_4_PRESENTATION, FunnelStep.STEP_5_CLOSING, FunnelStep.COMPLETED):
            return await self._cb_funnel_buy_now(user_id)
        return None

    # ── Private: WhatsApp menu handler ───────────────────────────────

    async def _handle_whatsapp_menu(self, user_id: str, text: str) -> list[OutgoingMessage] | None:
        """Handle numbered menu commands from WhatsApp. Returns None if not a menu command."""
        t = text.strip()
        if t == "1" or t.lower() in ("купить", "buy"):
            return await self._cb_funnel_buy_now(user_id)
        elif t == "2" or t.lower() in ("вопрос", "question"):
            return self._cb_funnel_question()
        elif t == "3" or t.lower() in ("отзывы", "reviews"):
            return self._cb_reviews()
        elif t == "4" or t.lower() in ("статьи", "articles"):
            return await self.handle_articles_command()
        elif t == "5" or t.lower() in ("скрининг", "здоровье", "screening"):
            return self._start_conversational_screening(user_id, "screening")
        elif t == "6" or t.lower() in ("whatsapp", "ватсап"):
            return [OutgoingMessage(
                text=f"💬 Для личной консультации напишите в WhatsApp:\n\n{self._settings.whatsapp_url}",
                parse_mode=None,
            )]
        elif t == "7" or t.lower() in ("заново", "restart", "start"):
            return await self.handle_start(user_id, platform="whatsapp")
        elif t.lower() in ("меню", "menu", "помощь", "help"):
            return [OutgoingMessage(text=WHATSAPP_MENU_TEXT, parse_mode=None)]
        return None

    # ── Private: conversational screening ────────────────────────────

    def _start_conversational_screening(self, user_id: str, test_type: str) -> list[OutgoingMessage]:
        if test_type == "capillary":
            question = self._screening.start_capillary(user_id)
        else:
            question = self._screening.start_screening(user_id)
        return [OutgoingMessage(text=question)]

    async def _handle_screening_answer(self, user_id: str, text: str) -> list[OutgoingMessage]:
        next_q, result = self._screening.process_answer(user_id, text)
        if next_q:
            return [OutgoingMessage(text=next_q)]
        if result:
            return await self.handle_screening_result(user_id, result)
        return [OutgoingMessage(text="Сессия скрининга истекла. Напишите 5 чтобы начать заново.", parse_mode=None)]

    # ── Private: button matching for WhatsApp ────────────────────────

    def _match_whatsapp_button(self, text: str) -> str | None:
        """Try to match WhatsApp button text to a callback_data string.

        WhatsApp sends the clean button text (no emojis, max 20 chars)
        when user taps a quick-reply button.
        """
        _BUTTON_MAP = {
            # With emojis (Telegram callback echo)
            "✅ готова узнать правду": "funnel:accept",
            "❌ не нужно": "funnel:reject",
            "🔬 в чём главная причина": "funnel:details",
            "🛒 хочу купить сейчас": "funnel:buy_now",
            "🛒 хочу купить": "funnel:buy_now",
            "🛒 готова оформить заказ": "funnel:buy_now",
            "🛒 оформить заказ": "funnel:buy_now",
            "❓ у меня вопрос": "funnel:question",
            "❓ есть сомнения": "funnel:objection",
            "💬 расскажу о своей ситуации": "funnel:to_qualification",
            "📋 покажите что поможет": "funnel:to_presentation",
            "📋 подробнее о продукте": "funnel:to_presentation",
            "🔬 научный фундамент": "funnel:science",
            "▶️ начать консультацию": "funnel:accept",
            "🔬 узнать причину и решение": "funnel:details",
            "💬 расскажу подробнее о себе": "funnel:to_qualification",
            "🧴 бальзам (порошок)": "buy_powder",
            "💊 бальзам (капсулы)": "buy_capsules",
            "🔙 назад": "menu_back",
            "🏠 главное меню": "menu_back",
            # Without emojis (WhatsApp quick-reply sends clean text)
            "готова узнать правду": "funnel:accept",
            "не нужно": "funnel:reject",
            "в чём главная причин": "funnel:details",
            "главная причина?": "funnel:details",
            "хочу купить сейчас": "funnel:buy_now",
            "хочу купить": "funnel:buy_now",
            "готова оформить заказ": "funnel:buy_now",
            "оформить заказ": "funnel:buy_now",
            "у меня вопрос": "funnel:question",
            "есть сомнения": "funnel:objection",
            "сомнения/вопросы": "funnel:question",
            "расскажу о своей сит": "funnel:to_qualification",
            "о своей ситуации": "funnel:to_qualification",
            "покажите что поможет": "funnel:to_presentation",
            "подробнее о продукте": "funnel:to_presentation",
            "научный фундамент": "funnel:science",
            "начать консультацию": "funnel:accept",
            "узнать причину": "funnel:details",
            "расскажу подробнее": "funnel:to_qualification",
            "бальзам (порошок)": "buy_powder",
            "бальзам (капсулы)": "buy_capsules",
            "назад": "menu_back",
            "главное меню": "menu_back",
            "задать вопрос": "funnel:question",
            # Test offer buttons (bridged via pending_test_offer state, not callback)
            "да, пройти тест": "test:yes",
            "да пройти тест": "test:yes",
            "нет, продолжим": "test:no",
            "нет продолжим": "test:no",
        }
        lower = text.strip().lower()
        if lower in _BUTTON_MAP:
            return _BUTTON_MAP[lower]
        # Partial match
        for btn_text, cb_data in _BUTTON_MAP.items():
            if lower == btn_text[:len(lower)] and len(lower) >= 5:
                return cb_data
        return None
