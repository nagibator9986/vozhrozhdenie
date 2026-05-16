from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from loguru import logger

from src.bot.handlers import router
from src.bot.middleware import ThrottlingMiddleware
from src.config import Settings
from src.services.articles_service import ArticlesService
from src.services.consultant_service import ConsultantService
from src.services.telegraph_service import TelegraphService
from src.services.video_service import VideoService


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )


def create_dispatcher(
    settings: Settings,
    consultant_service: ConsultantService,
    articles_service: ArticlesService,
    telegraph_service: TelegraphService | None = None,
    video_service: VideoService | None = None,
    flow_controller=None,
    telegram_messenger=None,
    **kwargs,
) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(ThrottlingMiddleware(settings=settings))
    dp.include_router(router)
    dp["settings"] = settings
    dp["consultant_service"] = consultant_service
    dp["articles_service"] = articles_service
    dp["telegraph_service"] = telegraph_service
    dp["video_service"] = video_service
    if flow_controller is not None:
        dp["flow_controller"] = flow_controller
    if telegram_messenger is not None:
        dp["telegram_messenger"] = telegram_messenger
    # Transcription service (optional, for voice messages)
    transcription_service = kwargs.get("transcription_service")
    if transcription_service is not None:
        dp["transcription_service"] = transcription_service
    return dp


async def on_startup(
    dispatcher: Dispatcher,
    consultant_service: ConsultantService,
    articles_service: ArticlesService,
    telegraph_service: TelegraphService | None = None,
    bot: Bot | None = None,
) -> None:
    logger.info("Bot starting up…")

    # Load article registry
    articles_service.load()
    logger.info(f"Articles loaded: {articles_service.count()} articles.")

    # Publish articles to Telegraph.
    # publish_all() is sync and calls time.sleep(1.2) between articles for
    # rate-limit compliance — running it directly here would block the event
    # loop (and Telegram polling) for ~1.2 s × N new articles on first start.
    # We offload it to a thread so the loop keeps serving updates.
    if telegraph_service and telegraph_service.is_available():
        try:
            logger.info("Publishing articles to Telegraph…")
            count = await asyncio.to_thread(
                telegraph_service.publish_all, articles_service.all()
            )
            total_cached = telegraph_service.cache_size()
            logger.info(
                f"Telegraph: {count} newly published, {total_cached} total cached."
            )
        except Exception as exc:
            logger.error(f"Telegraph publish_all failed: {exc}")

    # Register bot commands
    if bot:
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="🏠 Начать / перезапустить"),
                BotCommand(command="articles", description="📚 Научные статьи"),
                BotCommand(command="help", description="❓ Помощь"),
            ])
            logger.info("Bot commands registered.")
        except Exception as exc:
            logger.warning(f"Failed to set bot commands: {exc}")

    # Index knowledge base into ChromaDB
    try:
        logger.info("Initialising RAG knowledge base…")
        await consultant_service.index_knowledge_base(force=False)
        logger.info("RAG knowledge base ready.")
    except Exception as exc:
        logger.error(f"Failed to initialise RAG on startup: {exc}")


async def on_shutdown(dispatcher: Dispatcher) -> None:
    logger.info("Bot shutting down…")
