"""
Entry point for the Balsam Vozrozhdenie Telegram AI Chatbot.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _ensure_runtime_dirs() -> None:
    """Create the directories the bot writes to.

    Tolerates read-only filesystems gracefully — on hosts like Heroku/Render
    parts of the FS may be read-only, and a hard failure here would mask the
    real cause in logs. Any directory that cannot be created will fail
    loudly later when actually written to.

    Reads STATE_DIR / VIDEOS_DIR from env so prod (Railway with a volume
    mounted at /app/state) and local dev (defaults to ./data) work without
    code changes.
    """
    state = os.environ.get("STATE_DIR", "data")
    videos = os.environ.get("VIDEOS_DIR", "data/videos")
    for path in (
        state,
        f"{state}/chroma_db",
        videos,
        "knowledge_base",
        "knowledge_base/articles",
    ):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Print to stderr — the loguru file sink isn't set up yet.
            print(
                f"[startup] WARN: cannot create {path}: {exc}",
                file=sys.stderr,
            )


_ensure_runtime_dirs()

from loguru import logger  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from src.bot.bot import create_bot, create_dispatcher, on_shutdown, on_startup  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.flow.controller import FlowController  # noqa: E402
from src.repositories.conversation_repository import (  # noqa: E402
    Base,
    ConversationRepository,
    migrate_conversation_table,
)
from src.services.ai_factory import build_ai_service  # noqa: E402
from src.services.articles_service import ArticlesService  # noqa: E402
from src.services.consultant_service import ConsultantService  # noqa: E402
from src.services.content_catalog import ContentCatalog  # noqa: E402
from src.services.escalation_service import EscalationService  # noqa: E402
from src.services.idle_reminder_service import IdleReminderService  # noqa: E402
from src.services.rag_service import RAGService  # noqa: E402
from src.services.telegraph_service import TelegraphService  # noqa: E402
from src.services.transcription_service import TranscriptionService  # noqa: E402
from src.services.video_service import VideoService  # noqa: E402
from src.transport.telegram.messenger import TelegramMessenger  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logger.remove()
logger.add(
    sys.stderr,
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
        "<level>{message}</level>"
    ),
    colorize=True,
)
# Try to attach a file sink under state_dir. On read-only or otherwise
# unwritable filesystems (some PaaS free tiers) we silently skip the file
# sink rather than crashing — stderr logs above are enough.
_LOG_PATH = f"{os.environ.get('STATE_DIR', 'data')}/bot.log"
try:
    logger.add(
        _LOG_PATH,
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
    )
except (OSError, PermissionError) as exc:
    logger.warning(
        f"File logging disabled (cannot write to {_LOG_PATH}: {exc})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Loading configuration…")
    settings = get_settings()

    # ── Database ──────────────────────────────────────────────────────────
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    # For SQLite, switch to WAL journal mode + busy_timeout so concurrent
    # writers don't return "database is locked" under WhatsApp burst load.
    # WAL lets readers and writers run in parallel; busy_timeout makes the
    # writer wait up to 5 s instead of erroring instantly. These PRAGMAs are
    # no-ops on Postgres.
    if engine.dialect.name == "sqlite":
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
            await conn.execute(text("PRAGMA busy_timeout=5000"))
        logger.info("SQLite tuned: WAL + busy_timeout=5s")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Migration: add new columns + idle-scan index (idempotent on both
    # SQLite and Postgres).
    await migrate_conversation_table(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    logger.info("Database ready.")

    # ── Infrastructure / services ─────────────────────────────────────────
    repository = ConversationRepository(session_factory=session_factory)
    rag_service = RAGService(settings=settings)
    ai_service = build_ai_service(settings=settings)
    articles_service = ArticlesService(settings=settings)
    articles_service.load()  # ContentCatalog needs articles loaded synchronously
    telegraph_service = TelegraphService(access_token=settings.telegraph_access_token)

    content_catalog = ContentCatalog(
        settings=settings,
        articles=articles_service,
        telegraph=telegraph_service,
    )

    # Preload Telegraph cache so ConsultantService/ResponseEnforcer can
    # resolve article IDs to public URLs at send time.
    try:
        cached = telegraph_service.prime_cache()
        logger.info(f"Telegraph cache primed: {cached} entries.")
    except Exception as exc:
        logger.warning(f"Telegraph cache prime failed (continuing): {exc}")

    consultant_service = ConsultantService(
        repository=repository,
        ai_service=ai_service,
        rag_service=rag_service,
        settings=settings,
        articles_service=articles_service,
        content_catalog=content_catalog,
        telegraph_service=telegraph_service,
    )

    # ── Video service ────────────────────────────────────────────────────
    # videos_dir points at IMAGE-baked static content (read-only at runtime);
    # the Telegram file_id cache that VideoService writes lives next to it
    # so on Railway we'd want a writable spot — but the cache is only used
    # for Telegram (which mostly works fine if it can't write back).
    video_service = VideoService(videos_dir=settings.videos_dir)
    logger.info(f"VideoService ready (videos_dir={settings.videos_dir}).")

    # ── Transcription service (Whisper) ──────────────────────────────
    transcription_service = TranscriptionService(settings=settings)
    logger.info("TranscriptionService ready.")

    # ── Escalation service (operator handoff) ────────────────────────
    escalation_log_path = os.path.abspath(
        os.path.join(settings.state_dir, "escalations.log")
    )
    escalation_service = EscalationService(log_path=escalation_log_path)
    logger.info(f"EscalationService ready (log: {escalation_log_path}).")

    # ── Flow controller (shared across all channels) ─────────────────
    flow_controller = FlowController(
        consultant=consultant_service,
        settings=settings,
        articles=articles_service,
        telegraph=telegraph_service,
        escalation=escalation_service,
    )
    logger.info("FlowController ready.")

    # ── Telegram bot / dispatcher ────────────────────────────────────
    bot = create_bot(settings=settings)
    telegram_messenger = TelegramMessenger(bot=bot, video_service=video_service)

    dp = create_dispatcher(
        settings=settings,
        consultant_service=consultant_service,
        articles_service=articles_service,
        telegraph_service=telegraph_service,
        video_service=video_service,
        flow_controller=flow_controller,
        telegram_messenger=telegram_messenger,
        transcription_service=transcription_service,
    )

    async def _startup() -> None:
        await on_startup(dp, consultant_service, articles_service, telegraph_service, bot)

    async def _shutdown() -> None:
        await on_shutdown(dp)

    dp.startup.register(_startup)
    dp.shutdown.register(_shutdown)

    # ── Start: Telegram polling + optional Wazzup webhook ────────────
    try:
        if settings.wazzup_enabled and settings.wazzup_api_key:
            from src.transport.wazzup.client import WazzupClient
            from src.transport.wazzup.messenger import WazzupMessenger
            from src.transport.wazzup.webhook import WazzupWebhookServer

            wazzup_client = WazzupClient(
                api_key=settings.wazzup_api_key,
                channel_id=settings.wazzup_channel_id,
            )
            wazzup_messenger = WazzupMessenger(
                client=wazzup_client,
                video_base_url=settings.video_base_url,
            )
            wazzup_webhook = WazzupWebhookServer(
                flow_controller=flow_controller,
                messenger=wazzup_messenger,
                settings=settings,
                transcription_service=transcription_service,
            )
            # Idle reminder — soft nudge to silent clients after 1 hour.
            # Guaranteed once-per-session via _reminder_sent flag in DB.
            idle_reminder = IdleReminderService(
                repository=repository,
                messenger=wazzup_messenger,
                settings=settings,
            )
            wazzup_webhook.set_idle_reminder(idle_reminder)

            logger.info(
                f"Starting dual mode: Telegram polling + Wazzup webhook on port {settings.wazzup_webhook_port}"
            )
            await asyncio.gather(
                dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
                wazzup_webhook.start(port=settings.wazzup_webhook_port),
            )
        else:
            logger.info("Starting Telegram-only mode (Wazzup disabled)…")
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await engine.dispose()
        logger.info("Engine disposed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception as exc:
        logger.exception(f"Fatal error: {exc}")
        sys.exit(1)
