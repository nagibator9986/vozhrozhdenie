from __future__ import annotations

import json

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from src.bot.keyboards import (
    get_article_view_keyboard,
    get_articles_categories_keyboard,
    get_articles_paged_keyboard,
    get_back_to_menu_keyboard,
    get_both_tests_keyboard,
    get_buy_product_keyboard,
    get_capillary_keyboard,
    get_capsules_keyboard,
    get_funnel_closing_keyboard,
    get_funnel_details_keyboard,
    get_funnel_education_keyboard,
    get_funnel_filter_keyboard,
    get_funnel_presentation_keyboard,
    get_persistent_keyboard,
    get_purchase_keyboard,
    get_return_to_funnel_keyboard,
    get_reviews_keyboard,
    get_screening_keyboard,
    get_screening_result_keyboard,
)
from src.config import Settings
from src.domain.entities import FunnelStep
from src.services.articles_service import ArticlesService
from src.services.consultant_service import ConsultantService
from src.services.telegraph_service import TelegraphService
from src.services.video_service import VideoService

router = Router(name="main_router")

_TG_MAX_LEN = 4096

# ─────────────────────────────────────────────────────────────────────────────
# Funnel static texts (5 steps)
# ─────────────────────────────────────────────────────────────────────────────

FUNNEL_STEP_1 = (
    "Здравствуйте! Я — виртуальный помощник почётного академика Мурата Турлубекова.\n\n"
    "Сразу внесём ясность: *ЙодоТерапия* радикально отличается от официальной медицины.\n\n"
    "Если вы привыкли слепо доверять эндокринологам, маммологам и гинекологам — "
    "посмотрите на статистику: *80% из них сами имеют невылеченные узлы, фиброзы и кисты*, "
    "а их средняя продолжительность жизни — 54 года.\n\n"
    "Если вы ищете одобрения врача официальной медицины — эта консультация не для вас.\n\n"
    "Если вы готовы узнать, как *25 лет практики академика* и труды мирового учёного "
    "профессора Абрахама решают проблему 90% хронических заболеваний через высокие "
    "дозы йода — продолжим."
)

FUNNEL_REJECTED = (
    "Спасибо за ваше время. Желаем крепкого здоровья! 🙏\n\n"
    "Если передумаете — просто напишите /start"
)


FUNNEL_STEP_2_TEXT = (
    "Вероятно, вы здесь из-за проблем со *щитовидной железой, молочной железой "
    "или яичниками*.\n\n"
    "📌 *Официальные протоколы* лечения построены на наблюдении и росте ваших "
    "новообразований, итогом которого будет предложение хирурга удалить часть "
    "или весь орган.\n\n"
    "📌 *Наша база* — неопровержимые научные данные более *3000 учёных-экспертов* "
    "мирового уровня: огромный *дефицит йода* — главная причина появления и "
    "разрастания фиброзов, кист и узлов.\n\n"
    "Из-за нехватки йода у *97% людей* возникают проблемы со здоровьем.\n\n"
    "Когда в клетках не хватает йода, его место занимают *токсичные галогены* "
    "— Фтор, Хлор и Бром. Они ежедневно поступают из:\n"
    "• 🚰 *Хлор* — водопроводная вода, душ, бассейны, бытовая химия\n"
    "• 🦷 *Фтор* — зубные пасты, антипригарная посуда, кофе, чай\n"
    "• 🍞 *Бром* — мука, хлеб, пестициды, пластик, мягкая мебель\n\n"
    "Они накапливаются в клетках → мутации → кисты → узлы → фиброзы → онкология."
)


def _build_funnel_step_2_videos(settings: Settings) -> str:
    """Video links sent as a separate plain-text message (URLs contain _ which breaks Markdown)."""
    return (
        f"🎬 Видео профессора Нузума (США):\n{settings.video_nuzum_url}\n\n"
        f"🎬 Профессор Джон Грей о подмене йода:\n{settings.video_john_gray_url}\n\n"
        f"🎬 Профессор Браунштейн о развитии заболеваний:\n{settings.video_brownstein_url}"
    )


# ── Scripted "details" content (Gemini Msg 3-6 condensed into 2 messages) ────

FUNNEL_DETAILS_1 = (
    "Мы не переубеждаем тех, кто боится выйти за рамки аптечных мизерных "
    "дозировок йода.\n\n"
    "Официальная медицина запрещает йод внутрь больше 200 микрограмм в день, "
    "якобы выше — разрушает здоровье. *Мировые эксперты йода доказали обратное:* "
    "любое новообразование (фиброз, киста, узел) — это сигнал критического "
    "йодного голода тканями желёз.\n\n"
    "Мы восстанавливаем ткани эндокринных желёз большими дозами йода. "
    "Это единственный безопасный метод без операций, гормонов и лекарств.\n\n"
    "Эти токсичные галогены являются:\n"
    "• *Зобогенами* — растят опухоли\n"
    "• *Мутагенами* — ломают ДНК\n"
    "• *Онкогенами* — вызывают рак\n\n"
    "Они накапливаются в каждой вашей клетке и замещают йод, что приводит к:\n"
    "1️⃣ Сначала кисты — тело пытается изолировать яд\n"
    "2️⃣ Затем узлы, миомы и фиброзы\n"
    "3️⃣ Финальная стадия — онкология"
)

FUNNEL_DETAILS_2 = (
    "🔬 *Единственный выход:*\n\n"
    "Вы не можете изолироваться от внешнего мира, но вы можете «выбить» "
    "эти яды из своего тела.\n\n"
    "По законам химии, только *высокие дозы правильного йода* способны "
    "вытеснить бром, хлор и фтор из ваших рецепторов.\n\n"
    "Почему американские БАДы работают медленнее? В США более 30 наименований "
    "йодовых добавок принимают 3+ миллиона женщин, но восстановление занимает "
    "1.5-2 года. Их формулы не позволяют безопасно принимать высокие дозировки.\n\n"
    "*Бальзам Возрождение* — первое и единственное нанойодное соединение, "
    "где невозможна передозировка. Сроки восстановления: *3-6 месяцев*.\n\n"
    "Если вы делали *УЗИ* щитовидной и яичников или *маммографию* — "
    "нам будут нужны эти результаты для отслеживания динамики."
)

FUNNEL_STEP_3_INTRO = (
    "Спасибо, что продолжаете! Чтобы дать вам *точные рекомендации*, мне нужно "
    "узнать о вашей ситуации.\n\n"
    "Расскажите, пожалуйста:\n"
    "• Какой *диагноз* вам поставили? (мастопатия / узлы щитовидной / кисты яичников / другое)\n"
    "• *Как давно* обнаружили?\n"
    "• Есть ли результаты *УЗИ или маммографии*?\n"
    "• Какое *лечение* уже пробовали и каков результат?\n\n"
    "Эти данные помогут отследить динамику после начала курса бальзама."
)


FUNNEL_STEP_4_TEXT = (
    "🔬 *Единственный выход — высокие дозы правильного йода*\n\n"
    "По законам химии, только йод способен вытеснить бром, хлор и фтор из "
    "рецепторов ваших клеток.\n\n"
    "📋 *Бальзам Возрождение Plus (НаноЙод):*\n\n"
    "*Порошок* (быстрый результат):\n"
    "• 500 мг (500 000 мкг) йода в 1 чайной ложке\n"
    "• Курс: 3-6 месяцев → 90-99% результат\n"
    "• Стоимость: 750 000 тг за 3-мес. курс\n\n"
    "*Капсулы* (удобный приём):\n"
    "• 25 мг (25 000 мкг) в 1 капсуле, принимать 4 шт/день\n"
    "• Курс: 12-16 месяцев\n"
    "• Упаковки: 30 / 60 / 90 капсул (90 — выгоднее)\n\n"
    "📋 *Как принимать:*\n"
    "Утром натощак за 30 мин до еды: 1 ч.л. порошка в 100мл воды или 4 капсулы\n\n"
    "📋 *Обязательные кофакторы:*\n"
    "• Селен 200мкг — вместе с йодом (Т4→Т3)\n"
    "• Морская соль 1 ч.л. в 1.5л воды — через 1.5ч после йода\n"
    "• Магний — перед сном\n"
    "• Витамины В2 и В3\n"
    "• Витамин С 3-10г\n\n"
    "📋 *Что произойдёт:*\n"
    "✅ Рассасывание кист, узлов, фиброзов за 3-6 мес.\n"
    "✅ Восстановление гормонального фона без синтетических гормонов\n"
    "✅ Эффект омоложения на 10-15 лет\n\n"
    "Для сравнения: операция — от *1 000 000 тг* за ОДИН орган. "
    "Наш метод восстанавливает *все три органа* одновременно."
)


def _build_funnel_step_4_videos(settings: Settings) -> str:
    """Video links sent separately as plain text."""
    return (
        f"🎬 Как принимать бальзам:\n{settings.video_dosage_url}\n\n"
        f"🎬 Профессор Флечас о работе йода:\n{settings.video_flechas_url}"
    )


FUNNEL_SCIENCE_TEXT = (
    "🔬 *Научный фундамент Бальзама Возрождение*\n\n"
    "*1. Формула:* Соединение молекулярного йода с высокомолекулярными природными "
    "полисахаридами — нетоксичный, максимально биодоступный нутрицевтик.\n\n"
    "*2. Научный руководитель:* Академик РАН Торегельды Шарманов (1930–2024) — "
    "лауреат премии ВОЗ им. Леона Бернарда (высшая награда в мировом здравоохранении).\n\n"
    "*3. Международное признание:* Филипп Нобель, председатель Нобелевского братства, "
    "лично назвал проект *«фантастическим»* — стратегически важным для здоровья нации.\n\n"
    "*4. Безопасность:* Первое и единственное в мире нанойодное соединение, где "
    "*невозможна передозировка*. Излишки выводятся за сутки.\n\n"
    "*5. Опыт:* 25 лет, 500 000+ пользователей, команда 100+ учёных.\n\n"
    "Средняя норма йода — 100 000 мкг, признанная Британской энциклопедией ещё в 1911 году."
)


FUNNEL_STEP_5_TEXT = (
    "🏆 *Ваш выбор:*\n\n"
    "Вы можете годами ходить по кругу «обследование → наблюдение → операция» "
    "в обычных клиниках.\n\n"
    "Или вы можете дать своему телу то, в чём оно нуждается на клеточном уровне.\n\n"
    "Результат за 3-6 месяцев недоступен современной фармакологии. "
    "Это не чудо — это физиология, возвращённая в природную норму.\n\n"
    "Мы сопровождаем каждого клиента до результата."
)


def _build_funnel_step_5_contacts(settings: Settings) -> str:
    """Contacts sent separately as plain text."""
    return (
        f"🛒 Kaspi (рассрочка/кредит):\n{settings.kaspi_product_url}\n\n"
        f"🏢 Офис: {settings.office_address}\n\n"
        f"💬 WhatsApp: {settings.whatsapp_url}"
    )


HELP_TEXT = (
    "ℹ️ *Что я умею:*\n\n"
    "• Рассказать о Бальзаме Возрождение Plus\n"
    "• Объяснить протокол насыщения йодом по методу Абрахама\n"
    "• Ответить на вопросы о дозировках и кофакторах\n"
    "• Помочь при детокс-симптомах (кризис очищения)\n"
    "• Показать видеоотзывы клиентов\n"
    "• Отправить научные статьи\n"
    "• Помочь оформить заказ через Kaspi\n\n"
    "*Команды:*\n"
    "/start — начать заново\n"
    "/help — эта справка\n"
    "/articles — библиотека научных статей\n\n"
    "Или просто напишите вопрос — я отвечу!"
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_services(
    data: dict,
) -> tuple[ConsultantService, Settings, ArticlesService, TelegraphService | None, VideoService | None]:
    return (
        data["consultant_service"],
        data["settings"],
        data["articles_service"],
        data.get("telegraph_service"),
        data.get("video_service"),
    )


def _split_message(text: str, max_len: int = _TG_MAX_LEN) -> list[str]:
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


async def _send_response(message: Message, text: str) -> None:
    for part in _split_message(text):
        try:
            await message.answer(part, parse_mode="Markdown")
        except Exception:
            try:
                await message.answer(part, parse_mode=None)
            except Exception as exc:
                logger.error(f"Failed to send message: {exc}")


async def _send_response_with_video(
    message: Message,
    text: str,
    user_text: str,
    video_svc: VideoService | None,
) -> None:
    """Send AI response text, then smart-match and send relevant videos."""
    await _send_response(message, text)
    if video_svc:
        await video_svc.send_matched_videos(
            bot=message.bot,
            chat_id=message.chat.id,
            user_text=user_text,
            bot_response=text,
        )


async def _bot_send_chat_action(message: Message, action: str) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, action)
    except Exception:
        pass


async def _send_article(
    message: Message, article, telegraph: TelegraphService | None = None
) -> None:
    lang_note = " 🇬🇧" if article.language == "en" else ""
    intro = (
        f"📄 *{article.title}*{lang_note}\n\n"
        f"_{article.description}_\n\n"
        "Выберите способ просмотра:"
    )
    telegraph_url: str | None = None
    if telegraph and telegraph.is_available():
        telegraph_url = telegraph.get_url(article)
        if not telegraph_url:
            telegraph_url = telegraph.publish(article)
    await message.answer(
        intro,
        parse_mode="Markdown",
        reply_markup=get_article_view_keyboard(article.id, telegraph_url),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────


@router.message(Command("start"))
async def cmd_start(message: Message, **data) -> None:
    """Start or restart the funnel."""
    user_id = f"tg:{message.from_user.id}"
    logger.info(f"User {user_id} /start")
    consultant, settings, _, __, video_svc = _get_services(data)

    # Reset conversation and start funnel from step 1
    conversation = await consultant.get_or_create_conversation(user_id)
    conversation.funnel_step = FunnelStep.STEP_1_FILTER
    conversation.messages = []
    await consultant.save_conversation(conversation)

    # Send persistent keyboard
    await message.answer(
        "Добро пожаловать!", reply_markup=get_persistent_keyboard()
    )
    # Send funnel step 1
    await message.answer(
        FUNNEL_STEP_1,
        parse_mode="Markdown",
        reply_markup=get_funnel_filter_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, **data) -> None:
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@router.message(Command("articles"))
async def cmd_articles(message: Message, **data) -> None:
    _, __, articles, ___, _____ = _get_services(data)
    categories = articles.all_categories()
    if not categories:
        await message.answer("📚 Библиотека статей пока не загружена.")
        return
    await message.answer(
        "📚 *Научная библиотека*\n\nВыберите категорию:",
        parse_mode="Markdown",
        reply_markup=get_articles_categories_keyboard(categories),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Funnel callback handlers
# ─────────────────────────────────────────────────────────────────────────────


@router.callback_query(F.data == "funnel:accept")
async def cb_funnel_accept(callback: CallbackQuery, **data) -> None:
    """User accepted — move to step 2 (education)."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, settings, _, __, video_svc = _get_services(data)
    await callback.answer()

    conversation = await consultant.get_or_create_conversation(user_id)
    conversation.funnel_step = FunnelStep.STEP_2_EDUCATION
    await consultant.save_conversation(conversation)

    await callback.message.answer(
        FUNNEL_STEP_2_TEXT,
        parse_mode="Markdown",
    )

    # Send videos — funnel buttons attached to LAST video so they're always visible
    if video_svc:
        await video_svc.send_education_videos_step2(
            callback.bot, callback.message.chat.id,
            reply_markup=get_funnel_education_keyboard(),
        )
    else:
        await callback.message.answer(
            _build_funnel_step_2_videos(settings),
            parse_mode=None,
            reply_markup=get_funnel_education_keyboard(),
        )

    # Screening quiz — separate message after videos
    screening_url = settings.screening_url
    if screening_url:
        await callback.message.answer(
            "🔬 *Проверьте себя за 2 минуты* — узнайте, "
            "затронул ли дефицит йода ваш организм:",
            parse_mode="Markdown",
            reply_markup=get_both_tests_keyboard(screening_url),
        )


@router.callback_query(F.data == "funnel:reject")
async def cb_funnel_reject(callback: CallbackQuery, **data) -> None:
    """User rejected — end conversation."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, _, __, ___, _____ = _get_services(data)
    await callback.answer()

    conversation = await consultant.get_or_create_conversation(user_id)
    conversation.funnel_step = FunnelStep.REJECTED
    await consultant.save_conversation(conversation)

    await callback.message.answer(FUNNEL_REJECTED, parse_mode="Markdown")


@router.callback_query(F.data == "funnel:details")
async def cb_funnel_details(callback: CallbackQuery, **data) -> None:
    """Scripted content about halogens, consequences, and solution (Gemini Msg 3-6)."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, settings, _, __, _____ = _get_services(data)
    await callback.answer()

    # Send scripted details — part 1 (halogens + consequences)
    await callback.message.answer(
        FUNNEL_DETAILS_1,
        parse_mode="Markdown",
    )

    # Send scripted details — part 2 (solution + US comparison + UZI request)
    await callback.message.answer(
        FUNNEL_DETAILS_2,
        parse_mode="Markdown",
        reply_markup=get_funnel_details_keyboard(),
    )


@router.callback_query(F.data == "funnel:question")
async def cb_funnel_question(callback: CallbackQuery, **data) -> None:
    """User has a question — prompt them to type. They stay in the corridor."""
    await callback.answer()
    await callback.message.answer(
        "💬 Напишите ваш вопрос — я отвечу и мы продолжим!\n\n"
        "Вы можете спросить о:\n"
        "• Симптомах и диагнозах\n"
        "• Дозировках и кофакторах\n"
        "• Детокс-симптомах (кризис очищения)\n"
        "• Научных доказательствах\n"
        "• Стоимости и заказе",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "funnel:to_presentation")
async def cb_funnel_to_presentation(callback: CallbackQuery, **data) -> None:
    """Move to step 4 — presentation. Only if already qualified."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, settings, _, __, _____ = _get_services(data)
    await callback.answer()

    conversation = await consultant.get_or_create_conversation(user_id)

    # If user hasn't been through qualification — send them there first
    if conversation.funnel_step.value < FunnelStep.STEP_4_PRESENTATION.value:
        conversation.funnel_step = FunnelStep.STEP_4_PRESENTATION
        await consultant.save_conversation(conversation)

    await callback.message.answer(
        FUNNEL_STEP_4_TEXT,
        parse_mode="Markdown",
    )

    # Send videos embedded in chat
    video_svc = data.get("video_service")
    if video_svc:
        await video_svc.send_education_videos_step4(
            callback.bot, callback.message.chat.id,
            reply_markup=get_funnel_presentation_keyboard(),
        )
    else:
        await callback.message.answer(
            _build_funnel_step_4_videos(settings),
            parse_mode=None,
            reply_markup=get_funnel_presentation_keyboard(),
        )


@router.callback_query(F.data == "funnel:science")
async def cb_funnel_science(callback: CallbackQuery, **data) -> None:
    """Show science block."""
    await callback.answer()
    await callback.message.answer(
        FUNNEL_SCIENCE_TEXT,
        parse_mode="Markdown",
        reply_markup=get_funnel_presentation_keyboard(),
    )


@router.callback_query(F.data == "funnel:buy_now")
async def cb_funnel_buy_now(callback: CallbackQuery, **data) -> None:
    """Fast track to closing — from any step."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, settings, _, __, _____ = _get_services(data)
    await callback.answer()

    conversation = await consultant.get_or_create_conversation(user_id)
    conversation.funnel_step = FunnelStep.STEP_5_CLOSING
    await consultant.save_conversation(conversation)

    await callback.message.answer(
        FUNNEL_STEP_5_TEXT,
        parse_mode="Markdown",
    )
    await callback.message.answer(
        _build_funnel_step_5_contacts(settings),
        parse_mode=None,
        reply_markup=get_funnel_closing_keyboard(
            settings.kaspi_product_url,
            settings.kaspi_capsules_url,
            settings.whatsapp_url,
        ),
    )


@router.callback_query(F.data == "funnel:objection")
async def cb_funnel_objection(callback: CallbackQuery, **data) -> None:
    """User has doubts at closing — AI handles objections."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, _, __, ___, _____ = _get_services(data)
    await callback.answer()

    await _bot_send_chat_action(callback.message, "typing")
    typing_msg = await callback.message.answer("⏳ Подготавливаю ответ…")

    try:
        objection_text = "У меня есть сомнения перед покупкой. Помогите разобраться."
        response = await consultant.process_message(user_id, objection_text)
        await _send_response_with_video(
            callback.message, response, objection_text, data.get("video_service"),
        )
    except Exception as exc:
        logger.error(f"funnel:objection error for {user_id}: {exc}")
        await callback.message.answer("❌ Произошла ошибка. Напишите ваш вопрос.")
    finally:
        try:
            await typing_msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Menu callback handlers (buy, reviews, articles)
# ─────────────────────────────────────────────────────────────────────────────


@router.callback_query(F.data == "menu_buy")
async def cb_buy(callback: CallbackQuery, **data) -> None:
    await callback.answer()
    await callback.message.answer(
        "🛒 *Выберите форму Бальзам Возрождение Plus:*",
        parse_mode="Markdown",
        reply_markup=get_buy_product_keyboard(),
    )


@router.callback_query(F.data == "buy_powder")
async def cb_buy_powder(callback: CallbackQuery, **data) -> None:
    _, settings, __, ___, _____ = _get_services(data)
    await callback.answer()
    await callback.message.answer(
        "🧴 *Бальзам Возрождение Plus (порошок)*\n\n"
        "• 500 мг йода в 1 чайной ложке\n"
        "• Курс: 3-6 месяцев\n"
        "• Стоимость: *750 000 тг* (3-мес. курс)\n"
        "• Результат быстрее, чем капсулы\n\n"
        "Для сравнения: операция — от 1 000 000 тг за ОДИН орган.\n\n"
        f"🏢 Офис: {settings.office_address}",
        parse_mode="Markdown",
        reply_markup=get_purchase_keyboard(
            settings.kaspi_product_url,
            settings.whatsapp_url,
            settings.office_address,
        ),
    )


@router.callback_query(F.data == "buy_capsules")
async def cb_buy_capsules(callback: CallbackQuery, **data) -> None:
    _, settings, __, ___, _____ = _get_services(data)
    await callback.answer()
    await callback.message.answer(
        "💊 *Бальзам Возрождение Plus (капсулы)*\n\n"
        "• 25 мг йода в 1 капсуле (принимать 4 шт/день)\n"
        "• Курс: 12-16 месяцев\n"
        "• Упаковки: 30 / 60 / *90 капсул* (выгоднее)\n\n"
        "Заказать через Kaspi — рассрочка и кредит!",
        parse_mode="Markdown",
        reply_markup=get_capsules_keyboard(settings.kaspi_capsules_url),
    )


@router.callback_query(F.data == "menu_reviews")
async def cb_reviews(callback: CallbackQuery, **data) -> None:
    _, settings, __, ___, _____ = _get_services(data)
    await callback.answer()
    await callback.message.answer(
        "📹 *Видеоотзывы клиентов Бальзама Возрождение Plus*\n\n"
        "Реальные истории выздоровления:",
        parse_mode="Markdown",
        reply_markup=get_reviews_keyboard(settings.yandex_disk_reviews_url),
    )


@router.callback_query(F.data == "menu_articles")
async def cb_articles(callback: CallbackQuery, **data) -> None:
    _, __, articles, ___, _____ = _get_services(data)
    await callback.answer()
    categories = articles.all_categories()
    if not categories:
        await callback.message.answer("📚 Библиотека статей пока не загружена.")
        return
    await callback.message.answer(
        "📚 *Научная библиотека*\n\nВыберите категорию:",
        parse_mode="Markdown",
        reply_markup=get_articles_categories_keyboard(categories),
    )


@router.callback_query(F.data.startswith("articles_cat:"))
async def cb_articles_category(callback: CallbackQuery, **data) -> None:
    _, __, articles, ___, _____ = _get_services(data)
    await callback.answer()
    category = callback.data.split(":", 1)[1]
    cat_articles, total_pages = articles.list_by_category_paged(category, 0)
    if not cat_articles:
        await callback.message.answer("В этой категории статей нет.")
        return
    cat_label = dict(articles.all_categories()).get(category, category)
    page_info = f" — стр. 1/{total_pages}" if total_pages > 1 else ""
    await callback.message.answer(
        f"📁 *{cat_label}*{page_info}\n\nВыберите статью:",
        parse_mode="Markdown",
        reply_markup=get_articles_paged_keyboard(
            cat_articles, category, 0, total_pages
        ),
    )


@router.callback_query(F.data.startswith("articles_page:"))
async def cb_articles_page(callback: CallbackQuery, **data) -> None:
    _, __, articles, ___, _____ = _get_services(data)
    await callback.answer()
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        return
    _, category, page_str = parts
    try:
        page = int(page_str)
    except ValueError:
        return
    cat_articles, total_pages = articles.list_by_category_paged(category, page)
    if not cat_articles:
        return
    cat_label = dict(articles.all_categories()).get(category, category)
    page_info = f" — стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    new_text = f"📁 *{cat_label}*{page_info}\n\nВыберите статью:"
    new_markup = get_articles_paged_keyboard(cat_articles, category, page, total_pages)
    try:
        await callback.message.edit_text(
            new_text, parse_mode="Markdown", reply_markup=new_markup
        )
    except Exception:
        await callback.message.answer(
            new_text, parse_mode="Markdown", reply_markup=new_markup
        )


@router.callback_query(F.data == "articles_noop")
async def cb_articles_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("article_send:"))
async def cb_article_send(callback: CallbackQuery, **data) -> None:
    _, __, articles, telegraph, _____ = _get_services(data)
    await callback.answer()
    article_id = callback.data.split(":", 1)[1]
    article = articles.get_by_id(article_id)
    if not article:
        await callback.message.answer("❌ Статья не найдена.")
        return
    await _send_article(callback.message, article, telegraph)


@router.callback_query(F.data.startswith("article_file:"))
async def cb_article_file(callback: CallbackQuery, **data) -> None:
    _, __, articles, __, _____ = _get_services(data)
    await callback.answer("📎 Отправляю файл…")
    article_id = callback.data.split(":", 1)[1]
    article = articles.get_by_id(article_id)
    if not article:
        await callback.message.answer("❌ Статья не найдена.")
        return

    await _bot_send_chat_action(callback.message, "upload_document")
    lang_note = (
        "\n\n🇬🇧 _Статья на английском языке._" if article.language == "en" else ""
    )
    caption = f"📄 *{article.title}*\n\n_{article.description}_{lang_note}"

    original = article.read_original_bytes()
    if original:
        file_bytes, ext = original
        doc = BufferedInputFile(file_bytes, filename=f"{article.id}{ext}")
    else:
        text = article.read_text()
        if not text:
            await callback.message.answer("❌ Не удалось загрузить файл статьи.")
            return
        doc = BufferedInputFile(
            text.encode("utf-8"), filename=f"{article.id}.txt"
        )

    try:
        await callback.message.answer_document(
            doc, caption=caption, parse_mode="Markdown"
        )
    except Exception as exc:
        logger.error(f"Failed to send article file: {exc}")
        await callback.message.answer(f"❌ Не удалось отправить файл: {exc}")
        return

    await callback.message.answer(
        "Хотите посмотреть другие статьи?",
        reply_markup=get_back_to_menu_keyboard(),
    )


@router.callback_query(F.data == "menu_back")
async def cb_back(callback: CallbackQuery, **data) -> None:
    await callback.answer()
    await callback.message.answer(
        "Чем ещё могу помочь? Напишите вопрос или выберите действие.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Persistent keyboard button handlers
# ─────────────────────────────────────────────────────────────────────────────


@router.message(F.text == "🛒 Купить")
async def kb_buy(message: Message, **data) -> None:
    _, settings, __, ___, _____ = _get_services(data)
    await message.answer(
        FUNNEL_STEP_5_TEXT,
        parse_mode="Markdown",
    )
    await message.answer(
        _build_funnel_step_5_contacts(settings),
        parse_mode=None,
        reply_markup=get_funnel_closing_keyboard(
            settings.kaspi_product_url,
            settings.kaspi_capsules_url,
            settings.whatsapp_url,
        ),
    )


@router.message(F.text == "💬 Задать вопрос")
async def kb_question(message: Message, **data) -> None:
    await message.answer(
        "💬 Напишите ваш вопрос — я отвечу прямо сейчас!\n\n"
        "• Дозировки и кофакторы\n"
        "• Детокс-симптомы\n"
        "• Научные доказательства\n"
        "• Стоимость и заказ",
        parse_mode="Markdown",
    )


@router.message(F.text == "📹 Отзывы")
async def kb_reviews(message: Message, **data) -> None:
    _, settings, __, ___, _____ = _get_services(data)
    await message.answer(
        "📹 *Видеоотзывы клиентов Бальзама Возрождение Plus:*",
        parse_mode="Markdown",
        reply_markup=get_reviews_keyboard(settings.yandex_disk_reviews_url),
    )


@router.message(F.text == "📚 Научные статьи")
async def kb_articles(message: Message, **data) -> None:
    _, __, articles, ___, _____ = _get_services(data)
    categories = articles.all_categories()
    if not categories:
        await message.answer("📚 Библиотека статей пока не загружена.")
        return
    await message.answer(
        "📚 *Научная библиотека*\n\nВыберите категорию:",
        parse_mode="Markdown",
        reply_markup=get_articles_categories_keyboard(categories),
    )


@router.message(F.text == "💬 WhatsApp консультация")
async def kb_whatsapp(message: Message, **data) -> None:
    _, settings, __, ___, _____ = _get_services(data)
    await message.answer(
        f"💬 Для личной консультации напишите ассистенту академика "
        f"Мурата Турлубекова в WhatsApp:\n\n{settings.whatsapp_url}",
        parse_mode="Markdown",
    )


@router.message(F.text == "🔬 Проверить здоровье")
async def kb_screening(message: Message, **data) -> None:
    """Show both available health tests."""
    _, settings, __, ___, _____ = _get_services(data)
    screening_url = settings.screening_url
    if not screening_url:
        await message.answer(
            "🔬 Тесты здоровья временно недоступны. "
            "Напишите свои симптомы — я проведу анализ прямо здесь!",
        )
        return
    await message.answer(
        "🔬 *Экспресс-диагностика за 2 минуты*\n\n"
        "Выберите тест:\n\n"
        "📋 *Скрининг организма* — 19 маркеров клеточного голодания. "
        "Покажет, затронул ли дефицит йода ваш организм.\n\n"
        "👁️ *Тест капилляров* — осмотр белка глаз + слух + сосуды. "
        "Покажет реальный биологический возраст ваших сосудов.",
        parse_mode="Markdown",
        reply_markup=get_both_tests_keyboard(screening_url),
    )


@router.message(F.web_app_data)
async def handle_screening_result(message: Message, **data) -> None:
    """
    Handle screening quiz results from Telegram WebApp.

    Flow:
    1. Parse symptom list from WebApp data
    2. Send to LLM for personalized analysis (NOT hardcoded)
    3. LLM connects symptoms to thyroid/iodine deficiency
    4. Continue funnel with this context
    """
    user_id = f"tg:{message.from_user.id}"
    consultant, settings, _, __, video_svc = _get_services(data)

    try:
        result = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        logger.error(f"Bad screening data from user {user_id}")
        return

    result_type = result.get("type")
    if result_type not in ("screening_result", "capillary_result"):
        return

    # ── Parse results (works for both test types) ────────────────────
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

    # ── Build LLM prompt — personalized, NOT hardcoded ───────────────
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

    await _bot_send_chat_action(message, "typing")
    typing_msg = await message.answer("⏳ Анализирую ваши результаты…")

    try:
        response = await consultant.process_message(user_id, analysis_prompt)
        header = f"{test_emoji} *Результат теста {test_name}: {score}/{total}*\n\n"
        await _send_response(message, header + response)

        if video_svc:
            await video_svc.send_matched_videos(
                bot=message.bot,
                chat_id=message.chat.id,
                user_text=analysis_prompt,
                bot_response=response,
            )

    except Exception as exc:
        logger.error(f"{result_type} analysis error for {user_id}: {exc}")
        await message.answer(
            f"{test_emoji} *Результат теста: {score}/{total}*\n\n"
            "К сожалению, не удалось провести анализ. Расскажите о своих "
            "симптомах текстом — я помогу разобраться.",
            parse_mode="Markdown",
        )
    finally:
        try:
            await typing_msg.delete()
        except Exception:
            pass

    # ── Advance funnel ───────────────────────────────────────────────
    conversation = await consultant.get_or_create_conversation(user_id)
    if conversation.funnel_step.value < FunnelStep.STEP_3_QUALIFICATION.value:
        conversation.funnel_step = FunnelStep.STEP_3_QUALIFICATION
        await consultant.save_conversation(conversation)

    # After capillary test — offer the health screening too, and vice versa
    screening_url = settings.screening_url
    if is_capillary and screening_url:
        await message.answer(
            "Хотите пройти полный скрининг организма?",
            reply_markup=get_screening_keyboard(screening_url),
        )
    elif not is_capillary and screening_url:
        await message.answer(
            "Хотите проверить состояние капилляров по глазам?",
            reply_markup=get_capillary_keyboard(screening_url),
        )

    await message.answer(
        "Хотите узнать подробнее о причине и решении?",
        reply_markup=get_screening_result_keyboard(),
    )


@router.message(F.text == "🔄 Начать заново")
async def kb_reset(message: Message, **data) -> None:
    user_id = f"tg:{message.from_user.id}"
    consultant, _, __, ___, _____ = _get_services(data)
    try:
        await consultant.reset_conversation(user_id)
        await message.answer(
            "✅ *История очищена.* Напишите /start чтобы начать заново.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error(f"Reset failed for {user_id}: {exc}")
        await message.answer("❌ Не удалось очистить историю.")


# ─────────────────────────────────────────────────────────────────────────────
# Voice / audio message handler — transcribe then process as text
# ─────────────────────────────────────────────────────────────────────────────


@router.message(F.voice | F.audio)
async def handle_voice_message(message: Message, **data) -> None:
    """Transcribe voice/audio message via Whisper, then process as text."""
    import os
    import tempfile

    user_id = f"tg:{message.from_user.id}"
    transcription_svc = data.get("transcription_service")
    if not transcription_svc:
        await message.answer("🎤 Голосовые сообщения пока не поддерживаются.")
        return

    consultant, settings, _, __, video_svc = _get_services(data)

    await _bot_send_chat_action(message, "typing")
    typing_msg = await message.answer("🎤 Распознаю голосовое сообщение…")
    tmp_path: str | None = None

    try:
        # Download voice file
        voice = message.voice or message.audio
        file = await message.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await message.bot.download_file(file.file_path, tmp)
            tmp_path = tmp.name

        # Transcribe
        text = await transcription_svc.transcribe_file(tmp_path)

        if not text:
            await message.answer("❌ Не удалось распознать голосовое сообщение. Попробуйте написать текстом.")
            return

        logger.info(f"Voice from {user_id} transcribed: {text[:80]!r}")

        # Show what was recognized
        await message.answer(f"🎤 _{text}_", parse_mode="Markdown")

        # Process as regular text message
        response = await consultant.process_message(user_id, text)
        await _send_response_with_video(message, response, text, video_svc)

        # Return to funnel corridor
        conversation = await consultant.get_or_create_conversation(user_id)
        if conversation.funnel_step not in (FunnelStep.NOT_STARTED, FunnelStep.REJECTED):
            await message.answer(
                "Хотите продолжить?",
                reply_markup=get_return_to_funnel_keyboard(conversation.funnel_step.value),
            )
    except Exception as exc:
        logger.error(f"Voice processing error for {user_id}: {exc}")
        await message.answer("❌ Ошибка при обработке голосового. Попробуйте написать текстом.")
    finally:
        # Cleanup temp file
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        try:
            await typing_msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Generic text message handler — AI responds
# ─────────────────────────────────────────────────────────────────────────────


@router.message(F.text)
async def handle_text_message(message: Message, **data) -> None:
    """
    Universal text handler with corridor logic:
    1. NOT_STARTED → auto-start funnel
    2. STEP_3_QUALIFICATION → treat text as diagnosis info → advance to presentation
    3. Any other step → AI answers the question → return user to their funnel position

    Key principle: funnel_step does NOT change when user asks a question.
    The bot answers and then shows a "continue" button for the current step.
    """
    user_id = f"tg:{message.from_user.id}"
    user_text = (message.text or "").strip()
    if not user_text:
        return

    logger.info(f"User {user_id}: {user_text[:80]!r}")
    consultant, settings, _, __, _____ = _get_services(data)
    conversation = await consultant.get_or_create_conversation(user_id)
    current_step = conversation.funnel_step

    # ── NOT_STARTED → auto-start the funnel ──────────────────────────────
    if current_step == FunnelStep.NOT_STARTED:
        conversation.funnel_step = FunnelStep.STEP_1_FILTER
        conversation.messages = []
        await consultant.save_conversation(conversation)

        await message.answer(
            "Добро пожаловать!", reply_markup=get_persistent_keyboard()
        )
        await message.answer(
            FUNNEL_STEP_1,
            parse_mode="Markdown",
            reply_markup=get_funnel_filter_keyboard(),
        )
        return

    # ── REJECTED → offer to restart ──────────────────────────────────────
    if current_step == FunnelStep.REJECTED:
        await message.answer(
            "Если передумали — напишите /start чтобы начать заново.",
        )
        return

    # ── STEP_3 (Qualification) → text IS the diagnosis ───────────────────
    if current_step == FunnelStep.STEP_3_QUALIFICATION:
        await _bot_send_chat_action(message, "typing")
        typing_msg = await message.answer("⏳ Анализирую вашу ситуацию…")
        try:
            qual_prompt = (
                f"Клиент описывает свою ситуацию: {user_text}\n\n"
                "Дай персональный ответ на основе его диагноза. "
                "Объясни, как бальзам поможет конкретно в его случае. "
                "Упомяни дозировки и кофакторы."
            )
            response = await consultant.process_message(user_id, qual_prompt)
            await _send_response_with_video(
                message, response, user_text, data.get("video_service"),
            )
        except Exception as exc:
            logger.error(f"Qualification AI error for {user_id}: {exc}")
            await message.answer("❌ Произошла ошибка. Попробуйте ещё раз.")
        finally:
            try:
                await typing_msg.delete()
            except Exception:
                pass

        # Advance to presentation
        conversation = await consultant.get_or_create_conversation(user_id)
        conversation.funnel_step = FunnelStep.STEP_4_PRESENTATION
        await consultant.save_conversation(conversation)

        await message.answer(
            "Хотите узнать подробности о продукте и как его принимать?",
            reply_markup=get_funnel_presentation_keyboard(),
        )
        return

    # ── Detect affirmative replies ("да", "конечно", "хочу") ─────────────
    # If user types "да" instead of clicking a button, auto-advance the funnel
    lower_text = user_text.lower().strip()
    is_affirmative = lower_text in (
        "да", "конечно", "хочу", "давайте", "давай", "ок", "окей",
        "продолжим", "продолжить", "далее", "дальше", "го", "yes",
        "расскажите", "покажите", "хочу узнать", "интересно",
    )

    if is_affirmative:
        # Auto-advance to the next funnel step
        if current_step == FunnelStep.STEP_1_FILTER:
            # Simulate "Готова узнать правду" — same as cb_funnel_accept
            video_svc = data.get("video_service")
            conversation.funnel_step = FunnelStep.STEP_2_EDUCATION
            await consultant.save_conversation(conversation)
            await message.answer(FUNNEL_STEP_2_TEXT, parse_mode="Markdown")
            if video_svc:
                await video_svc.send_education_videos_step2(
                    message.bot, message.chat.id,
                    reply_markup=get_funnel_education_keyboard(),
                )
            else:
                await message.answer(
                    _build_funnel_step_2_videos(settings),
                    parse_mode=None,
                    reply_markup=get_funnel_education_keyboard(),
                )
            screening_url = settings.screening_url
            if screening_url:
                await message.answer(
                    "🔬 *Проверьте себя за 2 минуты:*",
                    parse_mode="Markdown",
                    reply_markup=get_both_tests_keyboard(screening_url),
                )
            return

        elif current_step == FunnelStep.STEP_2_EDUCATION:
            # Simulate "В чём главная причина?"
            await message.answer(FUNNEL_DETAILS_1, parse_mode="Markdown")
            await message.answer(
                FUNNEL_DETAILS_2,
                parse_mode="Markdown",
                reply_markup=get_funnel_details_keyboard(),
            )
            return

        elif current_step in (
            FunnelStep.STEP_4_PRESENTATION,
            FunnelStep.STEP_5_CLOSING,
            FunnelStep.COMPLETED,
        ):
            # Simulate "Оформить заказ"
            conversation.funnel_step = FunnelStep.STEP_5_CLOSING
            await consultant.save_conversation(conversation)
            await message.answer(FUNNEL_STEP_5_TEXT, parse_mode="Markdown")
            await message.answer(
                _build_funnel_step_5_contacts(settings),
                parse_mode=None,
                reply_markup=get_funnel_closing_keyboard(
                    settings.kaspi_product_url,
                    settings.kaspi_capsules_url,
                    settings.whatsapp_url,
                ),
            )
            return

    # ── ANY OTHER STEP → AI answers + smart video + return to corridor ───
    await _bot_send_chat_action(message, "typing")
    try:
        response = await consultant.process_message(user_id, user_text)
        await _send_response_with_video(
            message, response, user_text, data.get("video_service"),
        )

        # Return user to their corridor position
        await message.answer(
            "Хотите продолжить?",
            reply_markup=get_return_to_funnel_keyboard(current_step.value),
        )
    except Exception as exc:
        logger.exception(f"Error processing message for {user_id}: {exc}")
        await message.answer(
            "❌ Произошла ошибка. Попробуйте ещё раз или напишите /start."
        )


@router.callback_query(F.data == "funnel:to_qualification")
async def cb_funnel_to_qualification(callback: CallbackQuery, **data) -> None:
    """Explicit transition to qualification step."""
    user_id = f"tg:{callback.from_user.id}"
    consultant, _, __, ___, _____ = _get_services(data)
    await callback.answer()

    conversation = await consultant.get_or_create_conversation(user_id)
    conversation.funnel_step = FunnelStep.STEP_3_QUALIFICATION
    await consultant.save_conversation(conversation)

    await callback.message.answer(
        FUNNEL_STEP_3_INTRO,
        parse_mode="Markdown",
    )
