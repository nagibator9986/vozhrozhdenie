from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# ─────────────────────────────────────────────────────────────────────────────
# Persistent bottom keyboard (always visible)
# ─────────────────────────────────────────────────────────────────────────────

def get_persistent_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🛒 Купить"),
        KeyboardButton(text="💬 Задать вопрос"),
    )
    builder.row(
        KeyboardButton(text="📹 Отзывы"),
        KeyboardButton(text="📚 Научные статьи"),
    )
    builder.row(
        KeyboardButton(text="🔬 Проверить здоровье"),
        KeyboardButton(text="💬 WhatsApp консультация"),
    )
    builder.row(
        KeyboardButton(text="🔄 Начать заново"),
    )
    return builder.as_markup(resize_keyboard=True, persistent=True)


# ─────────────────────────────────────────────────────────────────────────────
# Funnel keyboards (steps 1–5)
# ─────────────────────────────────────────────────────────────────────────────

def get_funnel_filter_keyboard() -> InlineKeyboardMarkup:
    """Step 1: Filter — two choices."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Готова узнать правду",
            callback_data="funnel:accept",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Не нужно",
            callback_data="funnel:reject",
        )
    )
    return builder.as_markup()


def get_funnel_education_keyboard() -> InlineKeyboardMarkup:
    """Step 2: Education — curiosity-driven buttons."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔬 В чём главная причина болезней?",
            callback_data="funnel:details",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🛒 Хочу купить сейчас",
            callback_data="funnel:buy_now",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❓ У меня вопрос",
            callback_data="funnel:question",
        )
    )
    return builder.as_markup()


def get_funnel_details_keyboard() -> InlineKeyboardMarkup:
    """After details — lead to qualification or buy."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="💬 Расскажу о своей ситуации",
            callback_data="funnel:to_qualification",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🛒 Хочу купить сейчас",
            callback_data="funnel:buy_now",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❓ У меня вопрос",
            callback_data="funnel:question",
        )
    )
    return builder.as_markup()


def get_return_to_funnel_keyboard(step_value: int) -> InlineKeyboardMarkup:
    """
    Return the user to their current position in the funnel after an AI answer.
    Shows the appropriate 'continue' button based on where they are.
    """
    from src.domain.entities import FunnelStep

    builder = InlineKeyboardBuilder()

    if step_value <= FunnelStep.STEP_1_FILTER.value:
        builder.row(
            InlineKeyboardButton(
                text="▶️ Начать консультацию",
                callback_data="funnel:accept",
            )
        )
    elif step_value == FunnelStep.STEP_2_EDUCATION.value:
        builder.row(
            InlineKeyboardButton(
                text="🔬 В чём главная причина болезней?",
                callback_data="funnel:details",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="🛒 Хочу купить",
                callback_data="funnel:buy_now",
            )
        )
    elif step_value == FunnelStep.STEP_3_QUALIFICATION.value:
        builder.row(
            InlineKeyboardButton(
                text="💬 Расскажу о своей ситуации",
                callback_data="funnel:to_qualification",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="🛒 Хочу купить",
                callback_data="funnel:buy_now",
            )
        )
    elif step_value == FunnelStep.STEP_4_PRESENTATION.value:
        builder.row(
            InlineKeyboardButton(
                text="📋 Подробнее о продукте",
                callback_data="funnel:to_presentation",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="🛒 Готова оформить заказ",
                callback_data="funnel:buy_now",
            )
        )
    elif step_value >= FunnelStep.STEP_5_CLOSING.value:
        builder.row(
            InlineKeyboardButton(
                text="🛒 Оформить заказ",
                callback_data="funnel:buy_now",
            )
        )
    else:
        # COMPLETED or REJECTED — just offer buy
        builder.row(
            InlineKeyboardButton(
                text="🛒 Хочу купить",
                callback_data="funnel:buy_now",
            )
        )

    return builder.as_markup()


def get_funnel_qualification_keyboard() -> InlineKeyboardMarkup:
    """Step 3: After qualification — continue to presentation or buy."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📋 Покажите что поможет",
            callback_data="funnel:to_presentation",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🛒 Хочу купить сейчас",
            callback_data="funnel:buy_now",
        )
    )
    return builder.as_markup()


def get_funnel_presentation_keyboard() -> InlineKeyboardMarkup:
    """Step 4: After presentation — buy or ask more."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🛒 Готова оформить заказ",
            callback_data="funnel:buy_now",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔬 Научный фундамент",
            callback_data="funnel:science",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❓ Есть сомнения / вопросы",
            callback_data="funnel:question",
        )
    )
    return builder.as_markup()


def get_funnel_closing_keyboard(
    kaspi_url: str,
    capsules_url: str,
    whatsapp_url: str,
) -> InlineKeyboardMarkup:
    """Step 5: Closing — all purchase channels."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🛒 Купить на Kaspi (рассрочка/кредит)",
            url=kaspi_url,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💊 Капсулы на Kaspi",
            url=capsules_url,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💬 Консультация в WhatsApp",
            url=whatsapp_url,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❓ Есть сомнения",
            callback_data="funnel:objection",
        )
    )
    return builder.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Purchase / reviews / articles keyboards (kept from original)
# ─────────────────────────────────────────────────────────────────────────────

def get_buy_product_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🧴 Бальзам (порошок)", callback_data="buy_powder")
    )
    builder.row(
        InlineKeyboardButton(text="💊 Бальзам (капсулы)", callback_data="buy_capsules")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")
    )
    return builder.as_markup()


def get_capsules_keyboard(capsules_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📦 30 капсул", url=capsules_url))
    builder.row(InlineKeyboardButton(text="📦 60 капсул", url=capsules_url))
    builder.row(InlineKeyboardButton(text="📦 90 капсул (выгодно)", url=capsules_url))
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="menu_buy")
    )
    return builder.as_markup()


def get_purchase_keyboard(
    kaspi_url: str, whatsapp_url: str, office_address: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🛒 Купить на Kaspi", url=kaspi_url))
    builder.row(
        InlineKeyboardButton(text="💬 WhatsApp консультация", url=whatsapp_url)
    )
    builder.row(
        InlineKeyboardButton(text="💬 Задать вопрос", callback_data="funnel:question")
    )
    return builder.as_markup()


def get_reviews_keyboard(yandex_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📹 Смотреть отзывы", url=yandex_url))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back"))
    return builder.as_markup()


def get_articles_categories_keyboard(
    categories: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cat_key, cat_label in categories:
        builder.row(
            InlineKeyboardButton(
                text=cat_label,
                callback_data=f"articles_cat:{cat_key}",
            )
        )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back"))
    return builder.as_markup()


def get_articles_paged_keyboard(
    articles: list,
    category: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for art in articles:
        lang_flag = " 🇬🇧" if art.language == "en" else ""
        builder.row(
            InlineKeyboardButton(
                text=f"📄 {art.title}{lang_flag}",
                callback_data=f"article_send:{art.id}",
            )
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"articles_page:{category}:{page - 1}",
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="articles_noop",
            )
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"articles_page:{category}:{page + 1}",
                )
            )
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(text="🔙 К категориям", callback_data="menu_articles")
    )
    return builder.as_markup()


def get_article_view_keyboard(
    article_id: str,
    telegraph_url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if telegraph_url:
        builder.row(
            InlineKeyboardButton(text="📖 Читать онлайн", url=telegraph_url)
        )
    builder.row(
        InlineKeyboardButton(
            text="📎 Скачать файл",
            callback_data=f"article_file:{article_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔙 К категориям", callback_data="menu_articles")
    )
    return builder.as_markup()


def get_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_back")
    )
    return builder.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Screening WebApp
# ─────────────────────────────────────────────────────────────────────────────

def get_screening_keyboard(screening_url: str) -> InlineKeyboardMarkup:
    """Inline button that opens the screening quiz as a Telegram WebApp."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔬 Пройти скрининг за 2 минуты",
            web_app=WebAppInfo(url=screening_url),
        )
    )
    return builder.as_markup()


def get_capillary_keyboard(screening_url: str) -> InlineKeyboardMarkup:
    """Inline button that opens the capillary/eye test as a Telegram WebApp."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="👁️ Тест капилляров по глазам",
            web_app=WebAppInfo(url=screening_url.rstrip("/") + "/capillary"),
        )
    )
    return builder.as_markup()


def get_both_tests_keyboard(screening_url: str) -> InlineKeyboardMarkup:
    """Both tests in one keyboard — shown when user taps the persistent button."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔬 Скрининг организма (19 маркеров)",
            web_app=WebAppInfo(url=screening_url),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="👁️ Тест капилляров по глазам",
            web_app=WebAppInfo(url=screening_url.rstrip("/") + "/capillary"),
        )
    )
    return builder.as_markup()


def get_screening_result_keyboard() -> InlineKeyboardMarkup:
    """Buttons shown after screening results — continue funnel."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔬 Узнать причину и решение",
            callback_data="funnel:details",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💬 Расскажу подробнее о себе",
            callback_data="funnel:to_qualification",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🛒 Хочу купить сейчас",
            callback_data="funnel:buy_now",
        )
    )
    return builder.as_markup()
