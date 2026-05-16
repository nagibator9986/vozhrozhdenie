"""
ContentCatalog — central registry of everything the WhatsApp bot can send
to a client: videos, articles, reviews link, purchase links, screening URL.

Two functions:
1. build_prompt_section() — compact, LLM-readable list with keys.
   LLM can cite keys via [SEND:video_key1,video_key2] and [ATTACH:article_id1]
   tags in its response, and Python will turn them into real OutgoingMessage
   objects with fileable URLs.

2. resolve_send_tags() / resolve_attach_tags() — turn LLM tags into
   actual OutgoingMessage lists.

Design rationale:
- LLM picks content based on full context (not just regex on user_text)
- Python guarantees delivery — if LLM forgets a tag, we can still match by
  keyword fallback (helper functions provided)
- Relative video paths (e.g. "education/nuzum.mp4") are expanded to public URLs
  via settings.video_base_url (served by our own aiohttp static route over ngrok)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from loguru import logger

from src.config import Settings
from src.services.articles_service import ArticlesService
from src.services.telegraph_service import TelegraphService
from src.services.video_service import EDUCATION_VIDEOS, LIBRARY_VIDEOS
from src.transport.base import OutgoingMessage


# ── Tag regexes ──────────────────────────────────────────────────────────
_SEND_RE = re.compile(r"\[SEND:([a-zA-Z0-9_,\s]+)\]")
_ATTACH_RE = re.compile(r"\[ATTACH:([a-zA-Z0-9_,\s]+)\]")
_ADVANCE_RE = re.compile(r"\[ADVANCE:([0-6\-]+)\]")
_OFFER_RE = re.compile(r"\[OFFER:([A-Z_]+)\]")

# Hard size cap for WhatsApp video attachments (MB). Wazzup/WhatsApp
# typically refuses media > 16 MB.
_VIDEO_MAX_MB = 16


@dataclass
class VideoEntry:
    key: str              # short id used in [SEND:...] tags
    rel_path: str         # "education/nuzum.mp4" — relative to data/videos/
    caption: str
    topics: str           # human-readable topic description for LLM
    size_mb: float        # approximate file size


# ── Curated video list for WhatsApp ─────────────────────────────────────
# Only videos under 16 MB. Each entry has a clear topic line so the LLM
# can pick the right one for the conversation.
#
# Heavy videos (dosage.mp4 18MB, instruction_usage.mp4 52MB) are intentionally
# excluded — we send only the transcript/text for those topics.

_WA_VIDEOS: list[VideoEntry] = [
    # ── Education (the 4 professors + academic Turlubekov) ──
    VideoEntry(
        key="turlubekov_intro",
        rel_path="education/turlubekov_intro.mp4",
        caption="Академик Мурат Турлубеков — критические ошибки медицины",
        topics="когда клиент спрашивает про бота/центр/академика/кто мы/что за метод",
        size_mb=2.1,
    ),
    VideoEntry(
        key="nuzum",
        rel_path="education/nuzum.mp4",
        caption="Профессор Нузум (США) — дефицит йода у 97% людей",
        topics="когда клиент спрашивает ПОЧЕМУ болезни / в чём причина / дефицит",
        size_mb=6.9,
    ),
    VideoEntry(
        key="john_gray",
        rel_path="education/john_gray.mp4",
        caption="Профессор Джон Грей — подмена йода в гормонах T3/T4 галогенами",
        topics="галогены, фтор, хлор, бром, почему врачи против, гормоны T3 T4",
        size_mb=7.2,
    ),
    VideoEntry(
        key="brownstein",
        rel_path="education/brownstein.mp4",
        caption="Профессор Браунштейн — как развиваются узлы, кисты, мастопатия",
        topics="узлы, кисты, фиброзы, мастопатия, щитовидка, молочная железа, яичники",
        size_mb=4.0,
    ),
    VideoEntry(
        key="flechas",
        rel_path="education/flechas.mp4",
        caption="Профессор Флечас — как работает йод в больших дозах",
        topics="механизм йода, большие дозы, как помогает, доказательства",
        size_mb=1.6,
    ),
    VideoEntry(
        key="hearing_capillaries",
        rel_path="education/hearing_capillaries.mp4",
        caption="Слух как детектор состояния сосудов — омоложение капилляров",
        topics="слух, капилляры, сосуды, омоложение, микроциркуляция, зрение, память",
        size_mb=1.8,
    ),
    # ── Client reviews (library/all/...) ──
    VideoEntry(
        key="review_10y",
        rel_path="all/DWiK0OPkelL.mp4",
        caption="Отзыв: 10 лет пью йод — невероятные результаты",
        topics="отзывы, доказательства, долгий приём, опыт",
        size_mb=5.0,
    ),
    VideoEntry(
        key="review_mastopathy",
        rel_path="all/DWJbyXdioDb.mp4",
        caption="Отзыв: мастопатия не давала спать — ушла после курса",
        topics="отзыв мастопатия, молочная железа",
        size_mb=5.0,
    ),
    VideoEntry(
        key="review_pressure",
        rel_path="all/DWMB6gZEl-z.mp4",
        caption="Отзыв: 72 года, давление 250 → норма за 5 месяцев",
        topics="отзыв давление, гипертония, возраст",
        size_mb=5.0,
    ),
    VideoEntry(
        key="review_hypothyroid",
        rel_path="all/CyD0EiHtP9D.mp4",
        caption="Отзыв: исцеление гипотиреоза, мастопатии, эндометриоза",
        topics="отзыв гипотиреоз, эндометриоз",
        size_mb=5.0,
    ),
    # ── Diagnoses ──
    VideoEntry(
        key="diag_cysts_nodes",
        rel_path="all/DWaNFeRgiWU.mp4",
        caption="Кисты, узлы, фиброзы — учёные доказали: йод рассасывает",
        topics="узлы, кисты, фиброзы, рассасывание",
        size_mb=5.0,
    ),
    VideoEntry(
        key="diag_mastopathy",
        rel_path="all/CxKIJYTIQpg.mp4",
        caption="Мастопатия, кисты, фиброзы — рассасываются за 3-4 месяца (97% случаев)",
        topics="мастопатия, результат, рассасывание",
        size_mb=5.0,
    ),
    VideoEntry(
        key="diag_ait",
        rel_path="all/CxFlpQRMlMO.mp4",
        caption="АИТ — ключевая причина: дефицит йода",
        topics="АИТ, аутоиммунный тиреоидит, Хашимото",
        size_mb=5.0,
    ),
    VideoEntry(
        key="diag_thyroid",
        rel_path="all/CxFfarRsWvA.mp4",
        caption="Узлы, кисты щитовидки — что будет без йода",
        topics="узлы щитовидной, профилактика",
        size_mb=5.0,
    ),
    # ── Halogens (science) ──
    VideoEntry(
        key="halogen_chlorine",
        rel_path="all/DWOlUv4jNqu.mp4",
        caption="Хлор вытесняет йод из щитовидной железы",
        topics="хлор, вытеснение йода",
        size_mb=5.0,
    ),
    VideoEntry(
        key="halogen_chemicals",
        rel_path="all/DV3WDgPEY4M.mp4",
        caption="Химикаты разрушают здоровье — яды в еде и воде",
        topics="химикаты, токсины, отравление",
        size_mb=5.0,
    ),
    VideoEntry(
        key="science_1948",
        rel_path="all/DWfhTeKEYTy.mp4",
        caption="Научный подлог 1948: как занизили норму йода в 4500 раз",
        topics="ВОЗ, 150 мкг, подлог, 1948",
        size_mb=5.0,
    ),
    # ── Symptoms ──
    VideoEntry(
        key="symptoms_deficiency",
        rel_path="all/DWG3uz8D-Zx.mp4",
        caption="Симптомы дефицита йода: сухая кожа, выпадение волос",
        topics="симптомы дефицита, кожа, волосы",
        size_mb=5.0,
    ),
]

_WA_BY_KEY: dict[str, VideoEntry] = {v.key: v for v in _WA_VIDEOS}


@dataclass
class AttachmentBundle:
    """Everything Python has to send after the LLM text message."""
    videos: list[OutgoingMessage]         # separate messages with video_url
    articles: list[OutgoingMessage]       # separate messages with title+URL
    inline_additions: str                 # text snippets to append to main reply
    advance_to_step: int | None           # from [ADVANCE:N] tag
    offered_test: bool                    # from [OFFER:TEST]


class ContentCatalog:
    """Platform-aware content resolver.

    - build_wa_catalogue_text() → inject into system prompt for WhatsApp LLM
    - resolve(response, user_text, conv) → AttachmentBundle + cleaned text
    """

    def __init__(
        self,
        settings: Settings,
        articles: ArticlesService,
        telegraph: TelegraphService,
    ) -> None:
        self._settings = settings
        self._articles = articles
        self._telegraph = telegraph

    # ── System prompt injection ─────────────────────────────────────────

    def build_wa_catalogue_text(self) -> str:
        """Compact catalogue shown to the LLM so it knows what it can send."""
        lines = [
            "",
            "════════════════════════════════════════",
            "ТВОИ ИНСТРУМЕНТЫ ДОСТАВКИ (WhatsApp)",
            "════════════════════════════════════════",
            "",
            "Ты можешь прикладывать к своему ответу ВИДЕО, СТАТЬИ, ССЫЛКИ.",
            "Для этого в конце ответа добавляй служебные теги (клиент их не увидит).",
            "",
            "1. ВИДЕО — тег [SEND:key1,key2] (до 2 ключей за раз, каждое видео ≈5-8 МБ)",
            "   Используй когда текст не справляется — визуальное доказательство сильнее.",
            "",
            "   Доступные ключи:",
        ]
        for v in _WA_VIDEOS:
            lines.append(f"   • {v.key} — {v.caption}")
            lines.append(f"     когда использовать: {v.topics}")

        lines += [
            "",
            "2. СТАТЬИ — тег [ATTACH:article_id1,article_id2] (до 2 за раз)",
            "   Статьи — на Telegraph (мгновенный просмотр), клиент откроет одним тапом.",
            "",
            "   Доступные id (см. каталог статей ниже):",
        ]

        # Add all articles — URL resolved via self-hosted base or telegraph
        # at send-time. At prompt-time we just need the LLM to know the ids.
        have_articles_base = bool(self._settings.articles_base_url)
        all_articles: list[str] = []
        if self._articles:
            for art in self._articles._articles:
                has_url = have_articles_base or (
                    self._telegraph and self._telegraph.is_available()
                    and self._telegraph.get_url(art)
                )
                if has_url:
                    all_articles.append(f"   • {art.id} — {art.title}")
        if all_articles:
            lines.extend(all_articles[:30])  # cap to avoid prompt bloat
            if len(all_articles) > 30:
                lines.append(f"   … и ещё {len(all_articles) - 30} статей в полном каталоге ниже")
        else:
            lines.append("   (каталог пуст — прикладывай только видео)")

        lines += [
            "",
            "3. ССЫЛКИ НА ОТЗЫВЫ — тег [LINK:REVIEWS]",
            "   Когда клиент хочет увидеть реальные результаты других людей,",
            "   сомневается, требует доказательств.",
            "",
            "4. ССЫЛКИ НА ПОКУПКУ — тег [LINK:PURCHASE]",
            "   Когда клиент готов оформить заказ или явно спрашивает где купить.",
            "   Автоматически пришлёт: Kaspi порошок, Kaspi капсулы, адрес офиса.",
            "",
            "5. ТЕСТ ЗДОРОВЬЯ — тег [OFFER:TEST]",
            "   Предложи бесплатный 2-минутный тест если клиент неопределён,",
            "   колеблется или просит «что мне сделать». После тега клиент",
            "   получит ссылку на скрининг-сайт.",
            "",
            "6. ПРОДВИЖЕНИЕ ПО ВОРОНКЕ — тег [ADVANCE:N]",
            "   После того как ты уже выполнил цели текущего шага и клиент",
            "   готов двигаться дальше — запиши новый шаг.",
            "   1 = фильтр/приветствие, 2 = образование, 3 = квалификация,",
            "   4 = презентация, 5 = закрытие/ссылки на покупку, 6 = после-продажный Q&A",
            "   Только вперёд, никогда назад. Если шаг менять не нужно — не ставь тег.",
            "",
            "ПРАВИЛА:",
            "— Прикладывай видео ТОЛЬКО когда оно действительно релевантно —",
            "   не спамь. Одного хорошо подобранного видео достаточно.",
            "— Прикладывай статью когда обсуждаешь глубокую тему и клиенту",
            "   будет полезно почитать подробнее после твоего ответа.",
            "— [LINK:PURCHASE] ставь только когда клиент явно готов купить.",
            "— Теги — НА ОТДЕЛЬНЫХ СТРОКАХ в самом конце ответа, после всего текста.",
            "",
            "Пример хорошего ответа:",
            "«Ваши симптомы точно указывают на дефицит йода — в 94% случаев",
            "мастопатия II степени полностью рассасывается за 3-4 месяца",
            "на дозе 50 мг йода/день (данные Абрахама, 2002).",
            "",
            "Посмотрите короткое видео с профессором Браунштейном — он объясняет,",
            "как именно развиваются узлы и почему йод их рассасывает.",
            "",
            "Как давно у вас обнаружили мастопатию? Есть ли УЗИ?»",
            "",
            "[SEND:brownstein,diag_mastopathy]",
            "[ATTACH:mastopatiya_main]",
            "[SLOTS:diagnosis=мастопатия]",
            "[READY:4]",
            "[INTENT:NONE]",
        ]

        return "\n".join(lines)

    # ── Tag resolution ──────────────────────────────────────────────────

    def resolve(
        self,
        response: str,
        user_text: str,
    ) -> tuple[str, AttachmentBundle]:
        """Parse all catalogue tags out of response. Returns (clean_text, bundle)."""
        videos_out: list[OutgoingMessage] = []
        articles_out: list[OutgoingMessage] = []
        inline = ""
        advance_to: int | None = None
        offered_test = False

        # [SEND:k1,k2]
        send_keys: list[str] = []
        for m in _SEND_RE.finditer(response):
            for key in m.group(1).split(","):
                key = key.strip()
                if key and key not in send_keys:
                    send_keys.append(key)

        for key in send_keys[:2]:  # hard-cap at 2 videos to avoid spam
            v = _WA_BY_KEY.get(key)
            if not v:
                logger.warning(f"ContentCatalog: unknown video key '{key}'")
                continue
            if v.size_mb > _VIDEO_MAX_MB:
                logger.warning(f"ContentCatalog: skipping oversized video {key} ({v.size_mb} MB)")
                continue
            # WazzupMessenger sends text first (caption), then video_url as file.
            # Relative path is expanded via settings.video_base_url at send time.
            videos_out.append(OutgoingMessage(
                text=f"🎬 {v.caption}",
                video_url=v.rel_path,
                parse_mode=None,
            ))

        # [ATTACH:id1,id2]
        attach_ids: list[str] = []
        for m in _ATTACH_RE.finditer(response):
            for aid in m.group(1).split(","):
                aid = aid.strip()
                if aid and aid not in attach_ids:
                    attach_ids.append(aid)

        for aid in attach_ids[:2]:
            if not self._articles:
                break
            art = self._articles.get_by_id(aid)
            if not art:
                logger.warning(f"ContentCatalog: unknown article id '{aid}'")
                continue
            # Prefer self-hosted URL (works in Russia/Kazakhstan), fall
            # back to telegraph cache for legacy entries.
            url: str | None = None
            base = (self._settings.articles_base_url or "").rstrip("/")
            if base:
                url = f"{base}/{aid}"
            elif self._telegraph and self._telegraph.is_available():
                url = self._telegraph.get_url(art)
            if not url:
                logger.warning(f"ContentCatalog: no URL for article {aid}")
                continue
            articles_out.append(OutgoingMessage(
                text=f"📚 {art.title}\n{url}",
                parse_mode=None,
            ))

        # [LINK:REVIEWS], [LINK:PURCHASE] — handled inline (append to text)
        if re.search(r"\[LINK:REVIEWS\]", response):
            reviews_url = self._settings.yandex_disk_reviews_url
            if reviews_url:
                inline += f"\n\n📹 Видеоотзывы наших клиентов: {reviews_url}"

        if re.search(r"\[LINK:PURCHASE\]", response):
            inline += (
                f"\n\n🛒 Оформить Бальзам Возрождение Plus:"
                f"\n• Kaspi (порошок, рассрочка): {self._settings.kaspi_product_url}"
                f"\n• Kaspi (капсулы): {self._settings.kaspi_capsules_url}"
                f"\n• Офис в Алматы: {self._settings.office_address}"
                f"\n• WhatsApp для заказа/доставки: {self._settings.whatsapp_url}"
            )

        # [OFFER:TEST] — append screening URL
        if re.search(r"\[OFFER:TEST\]", response):
            offered_test = True
            url = self._settings.screening_url or ""
            if url and not url.startswith("http://localhost"):
                inline += (
                    f"\n\n🔬 Пройдите короткий тест здоровья (2 минуты) — "
                    f"он определит, в каком именно органе у вас дефицит йода "
                    f"и покажет персональные рекомендации:\n{url}"
                )
            else:
                # Fallback: no public URL configured — still acknowledge the offer
                # as a text-only option
                inline += (
                    "\n\n🔬 Могу прислать список из 19 симптомов — ответите "
                    "«да/нет» на каждый, и я скажу, какие системы у вас "
                    "в дефиците. Напишите «тест» чтобы начать."
                )

        # [ADVANCE:N]
        m = _ADVANCE_RE.search(response)
        if m:
            try:
                advance_to = int(m.group(1))
                if not 1 <= advance_to <= 6:
                    advance_to = None
            except ValueError:
                advance_to = None

        # Strip all catalogue tags from the user-visible text
        clean = _SEND_RE.sub("", response)
        clean = _ATTACH_RE.sub("", clean)
        clean = _ADVANCE_RE.sub("", clean)
        clean = _OFFER_RE.sub("", clean)
        clean = re.sub(r"\[LINK:(REVIEWS|PURCHASE)\]", "", clean)
        clean = "\n".join(line for line in clean.split("\n") if line.strip() or line == "")
        clean = clean.strip()

        return clean, AttachmentBundle(
            videos=videos_out,
            articles=articles_out,
            inline_additions=inline,
            advance_to_step=advance_to,
            offered_test=offered_test,
        )
