"""
ResponseEnforcer — post-LLM validation and proactive content injection.

Even with forced tool calling, LLMs are unreliable about optional schema
fields. gpt-4o-mini in particular almost never populates send_videos /
attach_articles / send_purchase_links. This layer enforces business rules
AFTER the LLM returns a structured response:

1. URL WHITELIST: strip any URL from `text` that isn't in our approved
   list (Kaspi product, Kaspi capsules, Yandex Disk reviews, office, WA
   operator, screening site, Telegraph articles). Prevents hallucinated
   fake-Kaspi URLs like the ones we saw in the wild.

2. PROACTIVE VIDEOS: at STEP_2 (education) and STEP_4 (presentation), if
   the LLM didn't attach any videos, we match the user_text + bot_text
   against VIDEO_TRIGGERS (17 regex patterns from VideoService) and
   inject up to 2 relevant videos. Clients don't get a "just text"
   answer at education steps — that's a dead sales funnel.

3. PROACTIVE REVIEWS: the first time a client expresses doubt or readiness
   ≥ 4, auto-send the Yandex Disk reviews URL exactly once.

4. PROACTIVE PURCHASE: at STEP_5, always send Kaspi links. If the bot
   mentions 750 000 / Kaspi / купить / оформить — send them.

5. OUT-OF-SCOPE DEFLECTION: if the user's diagnosis is outside our
   expertise (stomach cancer, lung cancer, skin melanoma, etc.), inject
   a honest disclaimer and downweight readiness. We don't push the
   product for conditions we can't credibly help with.

6. MIN-LENGTH RETRY: if the LLM returns text under 1200 chars, this
   module doesn't do the retry itself (that's the service's job), but
   it flags the response so the caller can re-prompt.

The enforcer is ONLY used in the WhatsApp path. Telegram keeps the
legacy free-form flow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.config import Settings
from src.domain.entities import FunnelStep
from src.services.articles_service import ArticlesService
from src.services.content_catalog import _WA_BY_KEY, _VIDEO_MAX_MB, VideoEntry
from src.services.telegraph_service import TelegraphService
from src.transport.base import OutgoingMessage


# ── URL WHITELIST ───────────────────────────────────────────────────────
# Very strict: we only allow EXACT short-link domains we actually operate.
# Full domain prefixes (e.g. "https://kaspi.kz/...") are BLOCKED because
# the LLM invents plausible-looking product URLs on kaspi.kz/shop/p/...
# that return 404. Real purchase links go only through settings whitelisted
# URLs injected by send_purchase_links=true.

_WHITELIST_HOST_PATTERNS: list[re.Pattern] = [
    re.compile(r"^l\.kaspi\.kz$", re.I),          # ONLY short links l.kaspi.kz/shop/<id>
    re.compile(r"^disk\.yandex\.(ru|com)$", re.I),
    re.compile(r"^yadi\.sk$", re.I),
    re.compile(r"^wa\.me$", re.I),
    re.compile(r"^telegra\.ph$", re.I),
    re.compile(r"^.+\.telegra\.ph$", re.I),
    re.compile(r"^.+\.ngrok-free\.dev$", re.I),    # our own video/articles host
    re.compile(r"^.+\.ngrok\.io$", re.I),
    re.compile(r"^testovozrazhdenie\.pythonanywhere\.com$", re.I),  # screening site
    re.compile(r"^.+\.pythonanywhere\.com$", re.I),  # generic pythonanywhere
]

# Any http(s)://... substring
_URL_RE = re.compile(r"https?://([^\s/:\)\]\}<>\"'»]+)[^\s\)\]\}<>\"'»]*")

# Legacy bracket-style tags that LLMs sometimes emit as literal text instead
# of using the tool call. Examples from production: [LINK:REVIEWS],
# [SEND:brownstein,diag_mastopathy], [ADVANCE:4], [SLOTS:diagnosis=рак].
# The prompt used to document these and the LLM remembers them across
# model versions. We strip them so clients never see system tags.
_BRACKET_TAG_RE = re.compile(
    r"\[(?:LINK|SEND|ATTACH|ADVANCE|OFFER|SLOTS|READY|INTENT)\s*:\s*[^\]]*\]",
    re.IGNORECASE,
)


def _is_whitelisted(url: str) -> bool:
    m = re.match(r"https?://([^/:\s]+)", url, re.I)
    if not m:
        return False
    host = m.group(1).lower()
    return any(p.match(host) for p in _WHITELIST_HOST_PATTERNS)


def strip_bracket_tags(text: str) -> tuple[str, list[str]]:
    """Remove any [TAG:value] system tags the LLM leaked into text.

    Returns (cleaned_text, list_of_stripped_tags).
    """
    removed = _BRACKET_TAG_RE.findall(text)
    if not removed:
        return text, []
    cleaned = _BRACKET_TAG_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), removed


def strip_markdown(text: str) -> str:
    """Convert markdown to plain text — WhatsApp doesn't render it.

    - **bold** / __bold__       → bold
    - *italic* / _italic_       → italic
    - [text](url)               → "text: url"  (URL preserved for whitelist)
    - `code`                    → code
    """
    if not text:
        return text
    # [text](url) — preserve URL so whitelist filter can decide
    text = re.sub(
        r"\[([^\]]+)\]\(\s*([^)\s]+)\s*\)",
        lambda m: f"{m.group(1)}: {m.group(2)}" if m.group(2).startswith("http") else m.group(1),
        text,
    )
    # **bold** and __bold__
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    # *italic* and _italic_ — careful not to break inflection
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    # `code`
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text


def strip_non_whitelisted_urls(text: str) -> tuple[str, list[str]]:
    """Remove URLs from text that aren't in the whitelist.

    Returns (cleaned_text, list_of_removed_urls).
    """
    removed: list[str] = []

    def _replace(m: re.Match) -> str:
        url = m.group(0)
        if _is_whitelisted(url):
            return url
        removed.append(url)
        return ""

    cleaned = _URL_RE.sub(_replace, text)
    # Also strip markdown-link wrappers like [text](url) if the url got nuked
    cleaned = re.sub(r"\[([^\]]*)\]\(\s*\)", r"\1", cleaned)
    # Collapse runs of whitespace introduced by removal
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), removed


# ── VIDEO AUTO-MATCHING ─────────────────────────────────────────────────
# Same regex triggers used by legacy VideoService but limited to keys that
# exist in our WhatsApp-safe _WA_BY_KEY catalogue (≤16 MB, public URLs).

_VIDEO_TRIGGERS: list[tuple[re.Pattern, list[str]]] = [
    # diagnosis → disease-specific videos
    (re.compile(r"мастопат|молочн.{0,10}желез|грудь", re.I),
     ["brownstein", "diag_mastopathy", "review_mastopathy"]),
    (re.compile(r"узел|узлы|щитовид|щж|тиреоид|гипотиреоз|гипертиреоз|аит|хашимото|зоб", re.I),
     ["brownstein", "diag_cysts_nodes", "diag_thyroid", "diag_ait", "review_hypothyroid"]),
    (re.compile(r"киста\b|кист.{0,5}яичник|яичник", re.I),
     ["brownstein", "diag_cysts_nodes"]),
    (re.compile(r"простат", re.I),
     ["brownstein", "diag_cysts_nodes"]),

    # mechanism / science
    (re.compile(r"галоген|фтор|хлор|бром|т3|т4", re.I),
     ["john_gray", "halogen_chlorine", "halogen_chemicals"]),
    (re.compile(r"как работает|как действ|механизм|большие дозы|доз", re.I),
     ["flechas"]),
    (re.compile(r"почему.{0,15}(болезн|дефицит|причин)|в чём причин", re.I),
     ["nuzum", "science_1948"]),

    # objections / proof
    (re.compile(r"отзыв|докажит|результат|реальн", re.I),
     ["review_10y", "review_mastopathy", "review_pressure"]),
    (re.compile(r"не верю|шарлатан|развод|клиническ|исследован", re.I),
     ["science_1948"]),
    (re.compile(r"врач.{0,15}(запрещ|против)|эндокринолог|опасно|воз|150 мкг|передозировк", re.I),
     ["john_gray", "science_1948"]),

    # cancer / oncology (carefully — no claims, just education)
    (re.compile(r"рак|онколог|метастаз|опухол|карцином", re.I),
     ["brownstein", "diag_cysts_nodes"]),

    # capillaries / aging / hearing
    (re.compile(r"слух|капилляр|сосуд|омоложен|старе|молоде", re.I),
     ["hearing_capillaries"]),

    # symptoms
    (re.compile(r"симптом|устал|волосы выпад|сухая кожа|холодн|слабость", re.I),
     ["symptoms_deficiency"]),
]


def match_videos_by_context(
    user_text: str,
    bot_text: str,
    already_sent: list[str] | None = None,
    max_videos: int = 1,
) -> list[str]:
    """Return up to N video keys matching the conversation context.

    Scans both user_text and bot_text — the bot might be talking about
    mastopathy even if the user message doesn't explicitly mention it.
    Already-sent videos are excluded to avoid repetition.
    """
    already_sent = already_sent or []
    haystack = f"{user_text}\n{bot_text}"
    picked: list[str] = []
    seen: set[str] = set(already_sent)

    for pattern, candidates in _VIDEO_TRIGGERS:
        if not pattern.search(haystack):
            continue
        for key in candidates:
            if key in seen:
                continue
            if key not in _WA_BY_KEY:
                continue
            v = _WA_BY_KEY[key]
            if v.size_mb > _VIDEO_MAX_MB:
                continue
            picked.append(key)
            seen.add(key)
            if len(picked) >= max_videos:
                return picked
    return picked


# ── ARTICLE AUTO-MATCHING ───────────────────────────────────────────────
# Diagnosis-to-article mapping — same pattern as _VIDEO_TRIGGERS.
# Each regex matches a diagnosis/topic in the conversation text, and maps
# to an ordered list of article IDs (best match first). This replaces the
# old full-text search which returned random articles because "йод" matched
# everything.

_ARTICLE_TRIGGERS: list[tuple[re.Pattern, list[str]]] = [
    # ── Diagnosis-specific ──
    (re.compile(r"мастопат|молочн.{0,10}желез|грудь|фиброаденом", re.I),
     ["mastopatiya_main", "mastopatiya_grud", "mastopatiya_novaya",
      "mastopatiya_sait", "zdorovye_grudi", "fatalnyy_error"]),

    (re.compile(r"узел|узлы|щитовид|щж|тиреоид|гипотиреоз|гипертиреоз|зоб", re.I),
     ["odin_klyuch", "zdorovye_grudi", "fatalnyy_error", "top50_bolezney"]),

    (re.compile(r"\bаит\b|хашимото|аутоиммунн\w*\s+тиреоид", re.I),
     ["ait", "odin_klyuch", "fatalnyy_error"]),

    (re.compile(r"киста|яичник|эндометриоз|миом", re.I),
     ["odin_klyuch", "top50_bolezney"]),

    (re.compile(r"простат", re.I),
     ["odin_klyuch", "top50_bolezney"]),

    (re.compile(r"артроз|сустав|колен|позвоноч|остеохондроз|хрящ", re.I),
     ["artroz"]),

    (re.compile(r"аутизм|ребён|ребенк|задержка\s+развит", re.I),
     ["autizm"]),

    (re.compile(r"деменц|альцгейм|память\s+ухудш|забывчив", re.I),
     ["dementsiya"]),

    (re.compile(r"лёгк|легк|бронх|астм|пневмон|кашл", re.I),
     ["legkie"]),

    (re.compile(r"витамин\s*[дd]|кальций|костн", re.I),
     ["vitamin_d"]),

    # ── Mechanism / science ──
    (re.compile(r"галоген|фтор|хлор|бром|замещен|вытесн", re.I),
     ["galogenovy_terror", "diktatura_galogenov", "ftor_hlor_brom",
      "biologicheskiy_terror"]),

    (re.compile(r"как принимат|схема|дозировк|кофактор|селен|магний", re.I),
     ["kak_prinimat", "27_effektov"]),

    (re.compile(r"почему.{0,15}(врач|запрещ|против)|эндокринолог.{0,10}(ошиб|запрет|не прав)",
                re.I),
     ["fatalnyy_error", "illyuziya"]),

    (re.compile(r"1948|подлог|воз\b|заниз|история\s+йод|правда", re.I),
     ["o_chem_ne_znaete", "illyuziya"]),

    # ── General iodine education ──
    (re.compile(r"симптом|устал|волосы|кожа|слабост|депресс|вес|отёк|отек", re.I),
     ["top50_bolezney", "27_effektov", "sistemy_organizma"]),

    (re.compile(r"горм\w*\s+дисбаланс|l[\-\s]?тироксин|эутирокс", re.I),
     ["mastopatiya_gormon", "fatalnyy_error"]),
]


def match_articles_by_context(
    user_text: str,
    bot_text: str,
    articles: ArticlesService,
    telegraph: TelegraphService | None,
    already_sent: list[str] | None = None,
    max_articles: int = 1,
) -> list[str]:
    """Return up to N article IDs relevant to the conversation context.

    Uses _ARTICLE_TRIGGERS (regex→article_ids mapping) instead of full-text
    search. Only returns IDs that actually exist in the articles library
    and haven't been sent yet.
    """
    already_sent = already_sent or []
    haystack = f"{user_text}\n{bot_text}"
    picked: list[str] = []
    seen: set[str] = set(already_sent)

    for pattern, candidates in _ARTICLE_TRIGGERS:
        if not pattern.search(haystack):
            continue
        for aid in candidates:
            if aid in seen:
                continue
            # Verify article exists in the library
            if articles.get_by_id(aid) is None:
                continue
            picked.append(aid)
            seen.add(aid)
            if len(picked) >= max_articles:
                return picked
    return picked


# ── OUT-OF-SCOPE DIAGNOSIS GUARD ────────────────────────────────────────
# Our expertise is iodine-responsive organs. Other cancers / conditions
# shouldn't get a "bals makes you better" pitch — it's dishonest.

_IN_SCOPE_PATTERNS = [
    re.compile(r"мастопат|молочн|груд", re.I),
    re.compile(r"щитовид|щж|тиреоид|гипотиреоз|гипертиреоз|аит|хашимото|зоб", re.I),
    re.compile(r"яичник|киста|эндометриоз|миом|фибром", re.I),
    re.compile(r"простат", re.I),
    re.compile(r"узл|фиброз|кист", re.I),
    re.compile(r"йод|дефицит|галоген|фтор|хлор|бром", re.I),
    re.compile(r"усталость|волос|кожа|слабост|депресс|память|слух|сосуд", re.I),
]

_OUT_OF_SCOPE_PATTERNS = [
    # Organ-specific cancers NOT in our scope
    re.compile(r"рак\s+(желудк|кишечник|поджелудочн|лёгк|легк|печен|почк|кров|мозг|кост)", re.I),
    re.compile(r"рак\s+(кож|меланом)", re.I),
    re.compile(r"лейкоз|лимфом(?!а\s+ходжк)", re.I),
    # Non-iodine conditions
    re.compile(r"диабет\s+1\s*тип|сахарн\w+\s+диабет\s+1", re.I),
    re.compile(r"вич|спид|гепатит\s+[вc]", re.I),
    re.compile(r"шизофрени|биполярн|аутизм", re.I),
    re.compile(r"инфаркт|инсульт\s+остр", re.I),
]


def detect_scope(text: str) -> str:
    """Returns 'in_scope', 'out_of_scope', or 'neutral'.

    Logic:
    - If any OUT pattern matches → 'out_of_scope'
    - Else if any IN pattern matches → 'in_scope'
    - Else → 'neutral'
    Out-of-scope takes priority because even if both match (e.g. "у меня
    рак желудка и ещё узлы щитовидки"), we should lead with the disclaimer.
    """
    if not text:
        return "neutral"
    for p in _OUT_OF_SCOPE_PATTERNS:
        if p.search(text):
            return "out_of_scope"
    for p in _IN_SCOPE_PATTERNS:
        if p.search(text):
            return "in_scope"
    return "neutral"


# ── TANGENT DETECTOR ────────────────────────────────────────────────────
# Client side-questions that take the conversation AWAY from their own
# problem: biography questions, general science, history. After answering
# these, the bot MUST ask a binary "return to funnel" question, not a
# passive "any more questions?".

_TANGENT_PATTERNS = [
    re.compile(r"кто\s+так(ой|ая|ие)|что\s+так(ой|ая|ое)|расскаж.*про(?!\s+мо)", re.I),
    re.compile(r"что\s+за\s+(метод|проект|центр|бальзам|йодотерап|наук)", re.I),
    re.compile(r"откуда|почему\s+врач|1948|вольф|варбург|абрахам\b|браунштейн|флечас|нузум", re.I),
    re.compile(r"история|подлог|фальсифик|занизили|воз\b|150\s*мкг|200\s*мкг", re.I),
    re.compile(r"кто\s+(при|ис?польз).*йод|какие.*учёные|учёный", re.I),
]


def detect_tangent(user_text: str) -> bool:
    """True if the user's last message is a tangent (off their own case)."""
    if not user_text:
        return False
    lower = user_text.lower()
    # Quick rejection: if user message mentions their own symptom/diagnosis,
    # it's not a tangent even if it also contains a science keyword.
    self_references = ("у меня", "моя", "мой", "мне", "у нас", "беспокоит", "болит", "ставит")
    has_self = any(s in lower for s in self_references)
    # A pure science question has neither "у меня" nor diagnosis keywords.
    if has_self:
        return False
    for p in _TANGENT_PATTERNS:
        if p.search(lower):
            return True
    return False


OUT_OF_SCOPE_DISCLAIMER = (
    "\n\nЧестно скажу: наша специализация — это состояния, напрямую "
    "связанные с дефицитом йода и галогенов в тканях: щитовидная железа, "
    "молочные железы, яичники, простата, мастопатия, узлы, кисты. "
    "По таким диагнозам у нас 25 лет опыта и чёткие научные данные. "
    "По вопросам, не связанным напрямую с йод-зависимыми органами, "
    "я не могу гарантировать конкретный результат от нашего протокола — "
    "в этих случаях бальзам может быть только общей поддержкой иммунитета "
    "и восстановления йодного статуса, но не основным лечением. "
    "Чтобы я мог быть максимально полезным — расскажите, есть ли у вас "
    "проблемы с щитовидной железой, молочными железами или обследования "
    "в этих областях?"
)


# ── DATA CLASS ──────────────────────────────────────────────────────────

@dataclass
class EnforcedResult:
    """Result of applying all enforcement rules to a structured LLM response."""
    text: str
    attachments: list[OutgoingMessage] = field(default_factory=list)
    readiness: int = 0
    intent: str = "NONE"
    slots: dict[str, str] = field(default_factory=dict)
    advance_to_step: int | None = None
    offered_test: bool = False
    removed_urls: list[str] = field(default_factory=list)
    stripped_tags: list[str] = field(default_factory=list)
    injected_videos: list[str] = field(default_factory=list)
    injected_articles: list[str] = field(default_factory=list)
    # ALL video keys / article IDs that ended up in attachments (LLM + auto-injected)
    sent_video_keys: list[str] = field(default_factory=list)
    sent_article_ids: list[str] = field(default_factory=list)
    is_out_of_scope: bool = False
    is_tangent: bool = False
    needs_length_retry: bool = False


# ── MAIN ENFORCER ───────────────────────────────────────────────────────

class ResponseEnforcer:
    """Applies business rules to an LLM structured response."""

    def __init__(
        self,
        settings: Settings,
        articles: ArticlesService,
        telegraph: TelegraphService | None,
        min_text_length: int = 1200,
    ) -> None:
        self._settings = settings
        self._articles = articles
        self._telegraph = telegraph
        self._min_text_length = min_text_length

    def _resolve_article_url(self, article) -> str | None:
        """Return a publicly reachable URL for this article (self-hosted only)."""
        base = (self._settings.articles_base_url or "").rstrip("/")
        if base:
            return f"{base}/{article.id}"
        return None

    def enforce(
        self,
        structured: dict[str, Any],
        user_text: str,
        funnel_step: FunnelStep,
        already_sent_videos: list[str] | None = None,
        already_sent_articles: list[str] | None = None,
        reviews_link_sent: bool = False,
        purchase_links_sent: bool = False,
    ) -> EnforcedResult:
        already_sent_videos = already_sent_videos or []
        already_sent_articles = already_sent_articles or []

        # ── 1. Extract basic fields ────────────────────────────────────
        raw_text: str = (structured.get("text") or "").strip()
        readiness: int = structured.get("readiness") or 0
        if isinstance(readiness, int):
            readiness = max(0, min(10, readiness))
        else:
            readiness = 0
        intent: str = (structured.get("intent") or "NONE") or "NONE"

        slots_raw = structured.get("slots") or {}
        slots: dict[str, str] = {}
        if isinstance(slots_raw, dict):
            for k, v in slots_raw.items():
                if v and isinstance(v, str) and v.lower() not in ("none", "null", "unknown", "-"):
                    slots[k] = v.strip()

        advance_to = structured.get("advance_to_step")
        if not isinstance(advance_to, int) or not (1 <= advance_to <= 6):
            advance_to = None

        llm_videos = structured.get("send_videos") or []
        if not isinstance(llm_videos, list):
            llm_videos = []
        llm_articles = structured.get("attach_articles") or []
        if not isinstance(llm_articles, list):
            llm_articles = []

        llm_wants_reviews = bool(structured.get("send_reviews_link"))
        llm_wants_purchase = bool(structured.get("send_purchase_links"))
        offered_test = bool(structured.get("offer_test"))

        # ── 2a. Strip bracket tags ([LINK:], [SEND:], etc) from text ──
        # LLMs sometimes emit legacy tags as literal text instead of using
        # the structured tool call. We always strip these.
        tag_cleaned, stripped_tags = strip_bracket_tags(raw_text)
        if stripped_tags:
            logger.warning(
                f"ResponseEnforcer stripped {len(stripped_tags)} leaked tags: {stripped_tags}"
            )

        # ── 2a.1. Strip markdown formatting ─────────────────────────
        # WhatsApp doesn't render *bold* / [text](url) / `code` properly.
        # The prompt tells the LLM to avoid markdown, but it sometimes
        # slips in **important** or [link](url) anyway.
        tag_cleaned = strip_markdown(tag_cleaned)

        # ── 2b. Strip fake URLs from text ─────────────────────────────
        clean_text, removed_urls = strip_non_whitelisted_urls(tag_cleaned)
        if removed_urls:
            logger.warning(
                f"ResponseEnforcer stripped {len(removed_urls)} bad URLs: {removed_urls}"
            )

        # ── 3. Tangent detector — side-question gating ────────────────
        # If the user asked a science/biography/history question instead
        # of about their own case, we'll (a) keep the answer educational,
        # (b) force a return-to-funnel question at the end, (c) not push
        # purchase links aggressively.
        is_tangent = detect_tangent(user_text)
        if is_tangent:
            logger.info("Tangent detected — user asked off-topic question")

        # ── 4. Out-of-scope guard ─────────────────────────────────────
        scope = detect_scope(user_text)
        is_out_of_scope = scope == "out_of_scope"
        if is_out_of_scope:
            logger.warning(f"Out-of-scope user message — injecting disclaimer")
            if OUT_OF_SCOPE_DISCLAIMER not in clean_text:
                clean_text += OUT_OF_SCOPE_DISCLAIMER
            readiness = min(readiness, 3)
            llm_wants_purchase = False  # don't push product
            llm_wants_reviews = False
            # Keep only educational videos, no direct diagnosis videos
            llm_videos = []
            llm_articles = []

        # ── 4. Proactive videos — STEP_2/4 if LLM empty ───────────────
        step_val = funnel_step.value
        injected_videos: list[str] = []
        final_videos: list[str] = []

        # Start with LLM-chosen videos (dedup vs already sent, cap at 1)
        for k in llm_videos:
            if isinstance(k, str) and k in _WA_BY_KEY and k not in already_sent_videos:
                final_videos.append(k)
            if len(final_videos) >= 1:
                break

        # If at education/presentation step and LLM provided no videos → inject
        # one most-relevant video via keyword match (was 2, now 1 for UX speed —
        # WhatsApp upload is slow and clients respond before second video lands)
        if not final_videos and step_val in (2, 3, 4) and not is_out_of_scope:
            auto = match_videos_by_context(
                user_text, clean_text,
                already_sent=already_sent_videos,
                max_videos=1,
            )
            if auto:
                logger.info(f"Enforcer injected videos at step {step_val}: {auto}")
                final_videos.extend(auto)
                injected_videos.extend(auto)

        # ── 5. Proactive articles — if glow topic and LLM empty ───────
        injected_articles: list[str] = []
        final_articles: list[str] = []
        for aid in llm_articles:
            if isinstance(aid, str) and aid not in already_sent_articles:
                final_articles.append(aid)
            if len(final_articles) >= 2:
                break

        if not final_articles and step_val in (2, 3, 4) and not is_out_of_scope and self._articles:
            auto = match_articles_by_context(
                user_text, clean_text, self._articles, self._telegraph,
                already_sent=already_sent_articles, max_articles=1,
            )
            if auto:
                logger.info(f"Enforcer injected article: {auto}")
                final_articles.extend(auto)
                injected_articles.extend(auto)

        # ── 6. Proactive reviews link ─────────────────────────────────
        # Logic:
        #   1. If user EXPLICITLY asks for reviews ("отзыв", "покажите"
        #      "есть ли") — ALWAYS send, even if already sent once.
        #      Client might have lost the link.
        #   2. Otherwise, once-per-conversation rule applies to prevent
        #      spamming the URL in every message.
        user_explicit_reviews = bool(
            re.search(
                r"\bотзыв|покаж.*отзыв|пришл.*отзыв|есть.{0,10}отзыв|"
                r"где.*отзыв|кто.*принима|реальн.*люд|результат.*друг|"
                r"доказательств|чем\s+докаж|где.*доказ|"
                r"кто\s+пробовал|успешн|есть.{0,10}пример|реальн.*случ",
                user_text.lower(),
            )
        )
        if is_out_of_scope:
            should_send_reviews = False
        elif user_explicit_reviews:
            should_send_reviews = True
        elif not reviews_link_sent:
            should_send_reviews = llm_wants_reviews or (
                intent in ("PRICE", "REVIEWS")
                or (readiness >= 5 and step_val >= 3)
            )
        else:
            should_send_reviews = False

        # ── 7. Purchase links — gated by intent ──────────────────────
        # Rules:
        #   1. NEVER send on REVIEWS/DETOX/PRICE intents — these are
        #      trust-building / objection-handling moments, not closing.
        #      Pushing Kaspi here is pressure selling.
        #   2. NEVER if user just asked for reviews (explicit request).
        #   3. At STEP_5 or readiness>=8 with PURCHASE intent — force it.
        #   4. LLM's own flag only honored if not in the above restrict list.
        #   5. De-dup via purchase_links_sent flag.
        purchase_blocked_by_intent = intent in ("REVIEWS", "DETOX", "PRICE")
        if is_out_of_scope or purchase_blocked_by_intent or user_explicit_reviews:
            should_send_purchase = False
        else:
            should_send_purchase = (
                llm_wants_purchase
                or step_val == 5
                or (readiness >= 8 and intent == "PURCHASE")
                or (step_val == 4 and readiness >= 8)
            ) and not purchase_links_sent

        # ── 8. Build attachments list ─────────────────────────────────
        attachments: list[OutgoingMessage] = []

        for key in final_videos[:1]:
            if key not in _WA_BY_KEY:
                logger.warning(f"ResponseEnforcer: video key '{key}' not in catalogue — skipping")
                continue
            v: VideoEntry = _WA_BY_KEY[key]
            if v.size_mb > _VIDEO_MAX_MB:
                continue
            attachments.append(OutgoingMessage(
                text=f"🎬 {v.caption}",
                video_url=v.rel_path,
                parse_mode=None,
            ))

        if self._articles:
            for aid in final_articles[:2]:
                art = self._articles.get_by_id(aid)
                if not art:
                    continue
                url = self._resolve_article_url(art)
                if not url:
                    continue
                attachments.append(OutgoingMessage(
                    text=f"📚 {art.title}\n{url}",
                    parse_mode=None,
                ))

        # ── 9. Inline links appended to main text ────────────────────
        inline_extras = ""

        if should_send_reviews:
            url = self._settings.yandex_disk_reviews_url
            if url and url not in clean_text:
                inline_extras += f"\n\n📹 Видеоотзывы наших клиентов: {url}"

        if should_send_purchase:
            kaspi = self._settings.kaspi_product_url
            if kaspi and kaspi not in clean_text:
                inline_extras += (
                    f"\n\n🛒 Оформить Бальзам Возрождение Plus:"
                    f"\n• Kaspi (порошок, рассрочка): {kaspi}"
                    f"\n• Kaspi (капсулы): {self._settings.kaspi_capsules_url}"
                    f"\n• Офис в Алматы: {self._settings.office_address}"
                    f"\n• WhatsApp для доставки: {self._settings.whatsapp_url}"
                )

        if offered_test:
            url = self._settings.screening_url or ""
            if url and url.startswith("http") and "localhost" not in url:
                inline_extras += (
                    f"\n\n🔬 Пройдите наш бесплатный 1-минутный тест здоровья — "
                    f"он за несколько вопросов покажет, какие системы вашего "
                    f"организма возможно в дефиците йода:\n{url}"
                )

        if inline_extras:
            clean_text += inline_extras

        # ── 10. Length check ──────────────────────────────────────────
        needs_length_retry = (
            len(clean_text) < self._min_text_length
            and not is_out_of_scope
            and step_val >= 2
        )

        return EnforcedResult(
            text=clean_text,
            attachments=attachments,
            readiness=readiness,
            intent=intent,
            slots=slots,
            advance_to_step=advance_to,
            offered_test=offered_test,
            removed_urls=removed_urls,
            stripped_tags=stripped_tags,
            injected_videos=injected_videos,
            injected_articles=injected_articles,
            # Authoritative lists of what was actually sent — used by caller for
            # dedup tracking. Avoids fragile reverse-lookup of Telegraph/ngrok URLs.
            sent_video_keys=list(final_videos[:1]),
            sent_article_ids=list(final_articles[:2]),
            is_out_of_scope=is_out_of_scope,
            is_tangent=is_tangent,
            needs_length_retry=needs_length_retry,
        )
