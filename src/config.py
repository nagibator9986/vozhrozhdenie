from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")

    # ── AI provider selection ─────────────────────────────────────────────
    # "anthropic" (Claude Sonnet 4.5, recommended) or "openai" (gpt-4o-mini / gpt-4o)
    ai_provider: str = Field(default="anthropic", alias="AI_PROVIDER")

    # ── OpenAI ────────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    model_name: str = Field(default="gpt-4o-mini", alias="MODEL_NAME")

    # ── Anthropic ────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL"
    )

    # ── Shared AI settings ───────────────────────────────────────────────
    max_response_tokens: int = Field(default=4096, alias="MAX_RESPONSE_TOKENS")
    ai_temperature: float = Field(default=0.3, alias="AI_TEMPERATURE")

    # ── External links — product ──────────────────────────────────────────
    kaspi_product_url: str = Field(
        default="https://l.kaspi.kz/shop/14ThrSXMrAWQ7VU",
        alias="KASPI_PRODUCT_URL",
    )
    kaspi_capsules_url: str = Field(
        default="https://kaspi.kz/your-capsules-link-here",
        alias="KASPI_CAPSULES_URL",
    )

    # ── External links — contacts ─────────────────────────────────────────
    whatsapp_url: str = Field(
        default="https://wa.me/77072886419",
        alias="WHATSAPP_URL",
    )
    office_address: str = Field(
        default=(
            "MTI Medical, г. Алматы, ул. Абиш Кикелбайулы, д. 254, "
            "блок 5, 1 этаж, вход отдельный, зелёная вывеска MTI Group"
        ),
        alias="OFFICE_ADDRESS",
    )

    # ── External links — videos ───────────────────────────────────────────
    video_nuzum_url: str = Field(
        default="https://www.instagram.com/p/CzByWariWzd/?hl=ru",
        alias="VIDEO_NUZUM_URL",
    )
    video_john_gray_url: str = Field(
        default="https://www.instagram.com/p/DVkisQGDezc/?hl=ru",
        alias="VIDEO_JOHN_GRAY_URL",
    )
    video_brownstein_url: str = Field(
        default="https://www.instagram.com/p/CxFkyFosZ_b/?hl=ru",
        alias="VIDEO_BROWNSTEIN_URL",
    )
    video_flechas_url: str = Field(
        default="https://www.instagram.com/p/CxHc7_AIOei/?hl=ru",
        alias="VIDEO_FLECHAS_URL",
    )
    video_dosage_url: str = Field(
        default="https://www.instagram.com/p/DV-_LLVjX9X/?hl=ru",
        alias="VIDEO_DOSAGE_URL",
    )

    # ── External links — reviews ──────────────────────────────────────────
    yandex_disk_reviews_url: str = Field(
        default="https://disk.yandex.ru/d/Ft8d4EBf8ZGv8A",
        alias="YANDEX_DISK_REVIEWS_URL",
    )

    # ── Screening WebApp ────────────────────────────────────────────────
    screening_url: str = Field(default="", alias="SCREENING_URL")

    # ── Telegraph ─────────────────────────────────────────────────────────
    telegraph_access_token: str = Field(default="", alias="TELEGRAPH_ACCESS_TOKEN")

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/bot.db",
        alias="DATABASE_URL",
    )

    # ── ChromaDB ──────────────────────────────────────────────────────────
    chroma_db_path: str = Field(default="./data/chroma_db", alias="CHROMA_DB_PATH")

    # ── Knowledge base ────────────────────────────────────────────────────
    knowledge_base_path: str = Field(
        default="./knowledge_base", alias="KNOWLEDGE_BASE_PATH"
    )

    # ── RAG chunking ──────────────────────────────────────────────────────
    rag_chunk_size: int = Field(default=600, alias="RAG_CHUNK_SIZE")
    rag_chunk_overlap: int = Field(default=80, alias="RAG_CHUNK_OVERLAP")

    # ── Conversation ──────────────────────────────────────────────────────
    max_history_messages: int = Field(default=20, alias="MAX_HISTORY_MESSAGES")

    # ── Rate limiting ─────────────────────────────────────────────────────
    throttle_rate: float = Field(default=1.5, alias="THROTTLE_RATE")

    # ── Backpressure (multi-user load shaping) ────────────────────────────
    # How many LLM round-trips may be in flight at once across ALL users.
    # Under a burst of N concurrent WhatsApp chats, only this many will hit
    # the model API; the rest wait on a semaphore. This bounds API spend,
    # avoids 429 cascades, and caps memory held for in-flight responses.
    # Tune up if you're on OpenAI Tier 2+/Anthropic enterprise; tune down
    # on cold-start free tiers.
    llm_max_concurrent: int = Field(default=20, alias="LLM_MAX_CONCURRENT")
    # Hard cap on a single LLM round-trip (seconds). A hung request would
    # otherwise pin a worker forever; aiohttp/asyncio do not timeout by
    # default at the SDK layer.
    llm_request_timeout: float = Field(default=90.0, alias="LLM_REQUEST_TIMEOUT")
    # Retries on 429/5xx with exponential backoff (1 = 1 retry = 2 attempts).
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")
    # How many SentenceTransformer encode() calls may run in parallel.
    # The model is a single in-process instance; too many parallel encodes
    # thrash the CPU and increase tail latency. Keep this small (2-4 on a
    # 1-2 vCPU host, 8 on a beefy box).
    rag_max_concurrent: int = Field(default=4, alias="RAG_MAX_CONCURRENT")
    # Maximum incoming webhook body size in bytes. Wazzup payloads are
    # tiny (KB), so a request bigger than this is either a misconfigured
    # client or an attacker trying to fill memory.
    webhook_max_body_size: int = Field(
        default=1_048_576,  # 1 MB
        alias="WEBHOOK_MAX_BODY_SIZE",
    )

    # ── Wazzup (WhatsApp) ─────────────────────────────────────────────────
    wazzup_api_key: str = Field(default="", alias="WAZZUP_API_KEY")
    wazzup_channel_id: str = Field(default="", alias="WAZZUP_CHANNEL_ID")
    wazzup_webhook_port: int = Field(default=8080, alias="WAZZUP_WEBHOOK_PORT")
    wazzup_webhook_path: str = Field(default="/wazzup/webhook", alias="WAZZUP_WEBHOOK_PATH")
    # Shared secret required to POST to the Wazzup webhook. Without it, anyone
    # who can reach the public URL can impersonate Wazzup and burn API credits.
    # In production this MUST be set. Accepted via either `Authorization:
    # Bearer <secret>` or `?token=<secret>` on the webhook URL.
    wazzup_webhook_secret: str = Field(default="", alias="WAZZUP_WEBHOOK_SECRET")
    wazzup_enabled: bool = Field(default=False, alias="WAZZUP_ENABLED")
    video_base_url: str = Field(default="", alias="VIDEO_BASE_URL")
    # Public base URL for self-hosted article HTML pages.
    # Replaces telegra.ph (blocked in Russia/Kazakhstan). See
    # WazzupWebhookServer._serve_article for the route.
    articles_base_url: str = Field(default="", alias="ARTICLES_BASE_URL")

    # ── Validators ────────────────────────────────────────────────────────
    @field_validator("max_history_messages")
    @classmethod
    def _validate_history(cls, v: int) -> int:
        if v < 4:
            raise ValueError("max_history_messages must be at least 4")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
