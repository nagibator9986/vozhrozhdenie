from __future__ import annotations

from src.config import Settings
from src.domain.entities import FunnelStep
from src.transport.base import Button


def get_funnel_filter_buttons() -> list[Button]:
    return [
        Button(text="✅ Готова узнать правду", callback_data="funnel:accept"),
        Button(text="❌ Не нужно", callback_data="funnel:reject"),
    ]


def get_funnel_education_buttons() -> list[Button]:
    return [
        Button(text="🔬 В чём главная причина болезней?", callback_data="funnel:details"),
        Button(text="🛒 Хочу купить сейчас", callback_data="funnel:buy_now"),
        Button(text="❓ У меня вопрос", callback_data="funnel:question"),
    ]


def get_funnel_details_buttons() -> list[Button]:
    return [
        Button(text="💬 Расскажу о своей ситуации", callback_data="funnel:to_qualification"),
        Button(text="🛒 Хочу купить сейчас", callback_data="funnel:buy_now"),
        Button(text="❓ У меня вопрос", callback_data="funnel:question"),
    ]


def get_funnel_qualification_buttons() -> list[Button]:
    return [
        Button(text="📋 Покажите что поможет", callback_data="funnel:to_presentation"),
        Button(text="🛒 Хочу купить сейчас", callback_data="funnel:buy_now"),
    ]


def get_funnel_presentation_buttons() -> list[Button]:
    return [
        Button(text="🛒 Готова оформить заказ", callback_data="funnel:buy_now"),
        Button(text="🔬 Научный фундамент", callback_data="funnel:science"),
        Button(text="❓ Есть сомнения / вопросы", callback_data="funnel:question"),
    ]


def get_funnel_closing_buttons(settings: Settings) -> list[Button]:
    return [
        Button(text="🛒 Купить на Kaspi (рассрочка/кредит)", url=settings.kaspi_product_url),
        Button(text="💊 Капсулы на Kaspi", url=settings.kaspi_capsules_url),
        Button(text="💬 Консультация в WhatsApp", url=settings.whatsapp_url),
        Button(text="❓ Есть сомнения", callback_data="funnel:objection"),
    ]


def get_return_to_funnel_buttons(step_value: int) -> list[Button]:
    if step_value <= FunnelStep.STEP_1_FILTER.value:
        return [Button(text="▶️ Начать консультацию", callback_data="funnel:accept")]
    elif step_value == FunnelStep.STEP_2_EDUCATION.value:
        return [
            Button(text="🔬 В чём главная причина болезней?", callback_data="funnel:details"),
            Button(text="🛒 Хочу купить", callback_data="funnel:buy_now"),
        ]
    elif step_value == FunnelStep.STEP_3_QUALIFICATION.value:
        return [
            Button(text="💬 Расскажу о своей ситуации", callback_data="funnel:to_qualification"),
            Button(text="🛒 Хочу купить", callback_data="funnel:buy_now"),
        ]
    elif step_value == FunnelStep.STEP_4_PRESENTATION.value:
        return [
            Button(text="📋 Подробнее о продукте", callback_data="funnel:to_presentation"),
            Button(text="🛒 Готова оформить заказ", callback_data="funnel:buy_now"),
        ]
    elif step_value >= FunnelStep.STEP_5_CLOSING.value:
        return [Button(text="🛒 Оформить заказ", callback_data="funnel:buy_now")]
    else:
        return [Button(text="🛒 Хочу купить", callback_data="funnel:buy_now")]


def get_buy_product_buttons() -> list[Button]:
    return [
        Button(text="🧴 Бальзам (порошок)", callback_data="buy_powder"),
        Button(text="💊 Бальзам (капсулы)", callback_data="buy_capsules"),
        Button(text="🔙 Назад", callback_data="menu_back"),
    ]


def get_capsules_buttons(capsules_url: str) -> list[Button]:
    return [
        Button(text="📦 30 капсул", url=capsules_url),
        Button(text="📦 60 капсул", url=capsules_url),
        Button(text="📦 90 капсул (выгодно)", url=capsules_url),
        Button(text="🔙 Назад", callback_data="menu_buy"),
    ]


def get_purchase_buttons(settings: Settings) -> list[Button]:
    return [
        Button(text="🛒 Купить на Kaspi", url=settings.kaspi_product_url),
        Button(text="💬 WhatsApp консультация", url=settings.whatsapp_url),
        Button(text="💬 Задать вопрос", callback_data="funnel:question"),
    ]


def get_reviews_buttons(yandex_url: str) -> list[Button]:
    return [
        Button(text="📹 Смотреть отзывы", url=yandex_url),
        Button(text="🔙 Назад", callback_data="menu_back"),
    ]


def get_articles_category_buttons(categories: list[tuple[str, str]]) -> list[Button]:
    buttons = []
    for cat_key, cat_label in categories:
        buttons.append(Button(text=cat_label, callback_data=f"articles_cat:{cat_key}"))
    buttons.append(Button(text="🔙 Назад", callback_data="menu_back"))
    return buttons


def get_article_view_buttons(article_id: str, telegraph_url: str | None = None) -> list[Button]:
    buttons = []
    if telegraph_url:
        buttons.append(Button(text="📖 Читать онлайн", url=telegraph_url))
    buttons.append(Button(text="📎 Скачать файл", callback_data=f"article_file:{article_id}"))
    buttons.append(Button(text="🔙 К категориям", callback_data="menu_articles"))
    return buttons


def get_screening_result_buttons() -> list[Button]:
    return [
        Button(text="🔬 Узнать причину и решение", callback_data="funnel:details"),
        Button(text="💬 Расскажу подробнее о себе", callback_data="funnel:to_qualification"),
        Button(text="🛒 Хочу купить сейчас", callback_data="funnel:buy_now"),
    ]


def get_back_to_menu_buttons() -> list[Button]:
    return [Button(text="🏠 Главное меню", callback_data="menu_back")]
