"""
VideoService — sends local .mp4 files as Telegram video messages.
Caches telegram file_id after first upload for instant re-sends.
Includes smart matching: picks relevant videos based on context.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile
from loguru import logger


import os as _os
# Telegram file_id cache. Writable at runtime, so lives under STATE_DIR
# (not under videos_dir which is image-baked / read-only on prod).
_CACHE_FILE = _os.path.join(
    _os.environ.get("STATE_DIR", "data"), "video_file_id_cache.json"
)

# ── Video Registry ───────────────────────────────────────────────────────
# Each video has: key, caption, and tags for smart matching

EDUCATION_VIDEOS = {
    "turlubekov_intro": {
        "key": "education/turlubekov_intro.mp4",
        "caption": "🎬 Академик Мурат Турлубеков — критические ошибки медицины",
        "tags": ["турлубеков", "академик", "метод", "кто вы", "расскажите"],
    },
    "nuzum": {
        "key": "education/nuzum.mp4",
        "caption": "🎬 Профессор Нузум (США) — дефицит йода у 97% людей",
        "tags": ["дефицит", "проблема", "причина", "почему"],
    },
    "john_gray": {
        "key": "education/john_gray.mp4",
        "caption": "🎬 Профессор Джон Грей — подмена йода в гормонах Т3 и Т4",
        "tags": ["галоген", "фтор", "хлор", "бром", "т3", "т4", "гормон", "замещен", "подмен", "врач", "против", "запрещ", "опасно", "воз"],
    },
    "brownstein": {
        "key": "education/brownstein.mp4",
        "caption": "🎬 Профессор Браунштейн — как развиваются заболевания",
        "tags": ["узел", "киста", "фиброз", "мастопат", "щитовид", "яичник", "молочн", "новообразован", "опухол", "развит"],
    },
    "flechas": {
        "key": "education/flechas.mp4",
        "caption": "🎬 Профессор Флечас — как работает йод в больших дозах",
        "tags": ["доз", "большие дозы", "как работает", "механизм", "действ", "помогает", "эффект"],
    },
    "dosage": {
        "key": "education/dosage.mp4",
        "caption": "🎬 Как принимать Бальзам Возрождение — инструкция",
        "tags": ["принимать", "приём", "прием", "дозировк", "инструкц", "порошок", "капсул", "кофактор", "селен", "соль", "магний"],
    },
    "hearing_capillaries": {
        "key": "education/hearing_capillaries.mp4",
        "caption": "🎬 Слух как детектор состояния сосудов — почему мы перестаём слышать молодость",
        "tags": ["слух", "слышать", "капилляр", "сосуд", "микроциркуляц", "кровообращен", "кровоток", "старени", "молодост", "омоложен"],
    },
}

# ── Extended library (downloaded from @iodinhealth.kz) ───────────────
# Organized by topic for smart matching

LIBRARY_VIDEOS = {
    # Отзывы
    "review_iodine_10y":    {"key": "all/DWiK0OPkelL.mp4", "caption": "🎬 Отзыв: 10 лет пью йод — невероятные результаты"},
    "review_pressure":      {"key": "all/DWMB6gZEl-z.mp4", "caption": "🎬 Отзыв: 72 года, давление 250 → нормализовалось за 5 месяцев"},
    "review_mastopathy":    {"key": "all/DWJbyXdioDb.mp4", "caption": "🎬 Отзыв: Мастопатия не давала спать — ушла после курса"},
    "review_son_saved":     {"key": "all/DWEVQCsDiP2.mp4", "caption": "🎬 Отзыв: Сын нашёл Возрождение — врачи были в шоке"},
    "review_hypothyroid":   {"key": "all/CyD0EiHtP9D.mp4", "caption": "🎬 Отзыв: Исцеление гипотиреоза, мастопатии, эндометриоза"},
    "review_hypothyroid2":  {"key": "all/CyBEFuytLUe.mp4", "caption": "🎬 Отзыв: Была больна гипотиреозом — за год исцелила"},
    "review_hyperthyroid":  {"key": "all/CxFnxqzsXn8.mp4", "caption": "🎬 Отзыв: Гипертиреоз — практика приёма, результаты"},
    # Галогены
    "halogen_chlorine":     {"key": "all/DWOlUv4jNqu.mp4", "caption": "🎬 Хлор вытесняет йод из щитовидной железы"},
    "halogen_veins":        {"key": "all/DV8xg7CiFod.mp4", "caption": "🎬 Хлор разъедает вены как кислота"},
    "halogen_chemicals":    {"key": "all/DV3WDgPEY4M.mp4", "caption": "🎬 Химикаты разрушают здоровье — яд в еде и воде"},
    "halogen_weapon":       {"key": "all/DVzIpRukrlY.mp4", "caption": "🎬 Хлор и бром — признанное химическое оружие"},
    # Наука / 1948
    "science_1948":         {"key": "all/DWfhTeKEYTy.mp4", "caption": "🎬 Научный подлог 1948 года: как занизили норму йода"},
    "science_truth":        {"key": "all/Cx-ugaRtkDh.mp4", "caption": "🎬 Скрытая правда о йодотерапии"},
    # Диагнозы
    "diag_cysts_nodes":     {"key": "all/DWaNFeRgiWU.mp4", "caption": "🎬 Кисты, узлы, фиброзы — учёные доказали: йод рассасывает"},
    "diag_fibrocysts":      {"key": "all/DWdD6iEGNFp.mp4", "caption": "🎬 Дефицит йода → фиброзные кисты и предраковые состояния"},
    "diag_mastopathy":      {"key": "all/CxKIJYTIQpg.mp4", "caption": "🎬 Мастопатия, кисты, фиброзы — за месяц их не будет"},
    "diag_ait":             {"key": "all/CxFlpQRMlMO.mp4", "caption": "🎬 АИТ — ключевая причина: дефицит йода"},
    "diag_thyroid_nodes":   {"key": "all/CxFfarRsWvA.mp4", "caption": "🎬 Узлы, кисты — что ожидает щитовидную без йода"},
    "diag_hormones":        {"key": "all/Cxuy78SNrIv.mp4", "caption": "🎬 Гормоны щитовидной — качественные ли они?"},
    "diag_15years":         {"key": "all/CxHcCknI_Ll.mp4", "caption": "🎬 15 лет у эндокринолога — здоровья нет. В чём дело?"},
    # Симптомы
    "symptoms_deficiency":  {"key": "all/DWG3uz8D-Zx.mp4", "caption": "🎬 Симптомы дефицита йода: сухая кожа, выпадение волос"},
    "symptoms_depression":  {"key": "all/CxFd-y7sKz9.mp4", "caption": "🎬 Нет депрессии, нет усталости — йодное питание"},
}

# Keyword patterns → which videos to send (max 1 per response)
VIDEO_TRIGGERS = [
    {
        "name": "who_are_you",
        "pattern": re.compile(
            r"кто вы|кто это|что за метод|расскажите о себе|что за проект|"
            r"что такое йодотерапи|что такое бальзам|турлубеков|академик|"
            r"что это|в чём суть|в чем суть|как это работает вообще",
            re.IGNORECASE,
        ),
        "videos": ["turlubekov_intro"],
        "max": 1,
    },
    {
        "name": "dosage_question",
        "pattern": re.compile(
            r"как приним|дозировк|инструкц|сколько приним|сколько пить|"
            r"как пить|кофактор|селен|магний|витамин|морская соль",
            re.IGNORECASE,
        ),
        "videos": ["dosage"],
        "max": 1,
    },
    {
        "name": "reviews_request",
        "pattern": re.compile(
            r"отзыв|видеоотзыв|кто приним|реальн|результат|доказательств|"
            r"покажите.{0,10}(отзыв|результат)|помогло кому",
            re.IGNORECASE,
        ),
        "videos": ["review_mastopathy", "review_hypothyroid", "review_pressure", "review_son_saved"],
        "max": 1,
    },
    {
        "name": "mastopathy_review",
        "pattern": re.compile(
            r"мастопат|молочн.{0,10}желез|грудь.{0,10}бол",
            re.IGNORECASE,
        ),
        "videos": ["review_mastopathy", "diag_mastopathy"],
        "max": 1,
    },
    {
        "name": "thyroid_diagnosis",
        "pattern": re.compile(
            r"узел|узлы|щитовид|щж|тиреоид",
            re.IGNORECASE,
        ),
        "videos": ["diag_cysts_nodes", "diag_thyroid_nodes"],
        "max": 1,
    },
    {
        "name": "cyst_ovary",
        "pattern": re.compile(
            r"киста|кист.{0,5}яичник|яичник",
            re.IGNORECASE,
        ),
        "videos": ["diag_fibrocysts"],
        "max": 1,
    },
    {
        "name": "ait_question",
        "pattern": re.compile(
            r"аит|аутоиммунн|хашимото|тиреоидит",
            re.IGNORECASE,
        ),
        "videos": ["diag_ait"],
        "max": 1,
    },
    {
        "name": "hypothyroid",
        "pattern": re.compile(
            r"гипотиреоз|гипо.{0,3}тиреоз",
            re.IGNORECASE,
        ),
        "videos": ["review_hypothyroid"],
        "max": 1,
    },
    {
        "name": "hyperthyroid",
        "pattern": re.compile(
            r"гипертиреоз|гипер.{0,3}тиреоз",
            re.IGNORECASE,
        ),
        "videos": ["review_hyperthyroid"],
        "max": 1,
    },
    {
        "name": "doctor_against",
        "pattern": re.compile(
            r"врач.{0,15}(запрещ|против|не рекоменд|нельзя)|"
            r"эндокринолог.{0,15}(запрещ|против|говорит)|"
            r"опасно|ВОЗ|150 мкг|200 мкг|передозировк",
            re.IGNORECASE,
        ),
        "videos": ["john_gray", "diag_15years"],
        "max": 1,
    },
    {
        "name": "skeptic_proof",
        "pattern": re.compile(
            r"не верю|не верит|шарлатан|развод|мошенн|клиническ|исследован",
            re.IGNORECASE,
        ),
        "videos": ["science_1948", "brownstein"],
        "max": 1,
    },
    {
        "name": "price_objection",
        "pattern": re.compile(
            r"доро[гж]|дорог\w*|д[оо]{2,}рог|"
            r"недёшев|недешев|кусает|кусаются|"
            r"космос.{0,15}цен|цен.{0,15}космос|космическ.{0,8}цен|"
            r"жесть.{0,8}цен|завышен|переплат|грабит|грабёж|"
            r"не по карман|не потяну|не осил|не.{0,15}позвол|"
            r"нет.{0,15}денег|денег.{0,15}нет|не хватает.{0,15}денег|"
            r"бюджет.{0,15}(не|маленьк|ограничен)|"
            r"зарплат.{0,15}(не|маленьк)|пенсион|"
            r"скидк|акци[яи]|промокод|рассрочк|в кредит|"
            r"почему так.{0,8}дорог|откуда.{0,15}цен|за что.{0,15}деньг|"
            r"750.{0,5}(тысяч|тыс|000)|"
            r"дешевл|подешевл|аналог.{0,15}(дешев|цен)|"
            r"денег.{0,8}жалко|жалко.{0,8}денег|копить|накопить|"
            r"(много|больш|так)\w*.{0,8}ден[еь]г|"
            r"слишком.{0,8}(много|дорог)|это.{0,5}много",
            re.IGNORECASE,
        ),
        "videos": ["review_iodine_10y", "review_son_saved"],
        "max": 1,
    },
    {
        "name": "halogens_question",
        "pattern": re.compile(
            r"галоген|фтор|хлор|бром|токсин|отравлен|яд",
            re.IGNORECASE,
        ),
        "videos": ["halogen_chlorine", "halogen_chemicals"],
        "max": 1,
    },
    {
        "name": "why_question",
        "pattern": re.compile(
            r"почему.{0,15}(болезн|заболев|дефицит|причин)|"
            r"в чём причин|причин.{0,10}заболев|откуда берут",
            re.IGNORECASE,
        ),
        "videos": ["nuzum", "science_truth"],
        "max": 1,
    },
    {
        "name": "symptoms_question",
        "pattern": re.compile(
            r"симптом|устал|депресс|волосы выпад|сухая кожа|отёк|отек|"
            r"холодн.{0,5}(руки|ноги)|слабость",
            re.IGNORECASE,
        ),
        "videos": ["symptoms_deficiency", "symptoms_depression"],
        "max": 1,
    },
    {
        "name": "hearing_vessels",
        "pattern": re.compile(
            r"слух|слышу|глохну|глухо|тугоухо|шум в уш|звон в уш|"
            r"капилляр|микроциркуляц|сосуд.{0,10}(забит|засор|плох)|"
            r"густая кровь|кровообращен|"
            r"старе[юн]|молоде[юн]|омоложен|выгляж.{0,8}молод|"
            r"биологическ.{0,8}возраст",
            re.IGNORECASE,
        ),
        "videos": ["hearing_capillaries"],
        "max": 1,
    },
    {
        "name": "1948_question",
        "pattern": re.compile(
            r"1948|подлог|фальсифик|занизили|вольф|чайков",
            re.IGNORECASE,
        ),
        "videos": ["science_1948"],
        "max": 1,
    },
    {
        "name": "how_it_works",
        "pattern": re.compile(
            r"как работает|как действ|как помогает|механизм|что происход",
            re.IGNORECASE,
        ),
        "videos": ["flechas"],
        "max": 1,
    },
    {
        "name": "video_request",
        "pattern": re.compile(
            r"видео|покажите|ролик|посмотреть",
            re.IGNORECASE,
        ),
        "videos": ["nuzum"],
        "max": 1,
    },
]


class VideoService:
    """Manages local video files and sends them via Telegram Bot API."""

    def __init__(self, videos_dir: str = "data/videos"):
        self._videos_dir = Path(videos_dir)
        self._cache_path = Path(_CACHE_FILE)
        self._file_id_cache: dict[str, str] = {}
        self._load_cache()

    # ── Cache management ─────────────────────────────────────────────────

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._file_id_cache = json.loads(self._cache_path.read_text())
                logger.info(f"VideoService: loaded {len(self._file_id_cache)} cached file_ids")
            except Exception:
                self._file_id_cache = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._file_id_cache, ensure_ascii=False, indent=2))
        except Exception as exc:
            logger.warning(f"VideoService: failed to save cache: {exc}")

    # ── Smart matching ───────────────────────────────────────────────────

    def _lookup_video(self, name: str) -> dict | None:
        """Find video by name in both registries."""
        vid = EDUCATION_VIDEOS.get(name) or LIBRARY_VIDEOS.get(name)
        if vid and self.has_video(vid["key"]):
            return vid
        return None

    def match_videos(self, user_text: str, bot_response: str) -> list[dict]:
        """
        Determine which videos to send based on USER message only.
        Bot response is NOT used for matching — it would trigger too many false positives.
        Returns max 1 video per response to avoid spam.
        """
        text = user_text.lower()

        for trigger in VIDEO_TRIGGERS:
            if trigger["pattern"].search(text):
                for vid_name in trigger["videos"]:
                    vid = self._lookup_video(vid_name)
                    if vid:
                        return [vid]  # First match wins — 1 video max
        return []

    # ── Send single video ────────────────────────────────────────────────

    async def send_video(
        self,
        bot: Bot,
        chat_id: int,
        video_key: str,
        caption: Optional[str] = None,
        reply_markup=None,
    ) -> bool:
        """Send a video to chat. Returns True on success."""
        # Try cached file_id first (instant send)
        cached_id = self._file_id_cache.get(video_key)
        if cached_id:
            try:
                await bot.send_video(
                    chat_id=chat_id,
                    video=cached_id,
                    caption=caption,
                    reply_markup=reply_markup,
                )
                return True
            except Exception:
                del self._file_id_cache[video_key]
                logger.warning(f"VideoService: stale cache for {video_key}, re-uploading")

        # Upload from file
        file_path = self._videos_dir / video_key
        if not file_path.exists():
            logger.error(f"VideoService: file not found: {file_path}")
            return False

        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        if file_size_mb > 50:
            logger.warning(f"VideoService: {video_key} is {file_size_mb:.1f}MB (>50MB limit)")

        try:
            video_file = FSInputFile(str(file_path))
            msg = await bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                reply_markup=reply_markup,
            )
            if msg.video:
                self._file_id_cache[video_key] = msg.video.file_id
                self._save_cache()
                logger.info(f"VideoService: uploaded & cached {video_key}")
            return True
        except Exception as exc:
            logger.error(f"VideoService: failed to send {video_key}: {exc}")
            return False

    # ── Batch send matched videos ────────────────────────────────────────

    async def send_matched_videos(
        self,
        bot: Bot,
        chat_id: int,
        user_text: str,
        bot_response: str,
    ) -> int:
        """
        Smart send: match videos based on context and send them.
        Returns number of videos sent.
        """
        videos = self.match_videos(user_text, bot_response)
        if not videos:
            return 0

        count = 0
        for vid in videos:
            ok = await self.send_video(bot, chat_id, vid["key"], caption=vid["caption"])
            if ok:
                count += 1

        if count:
            logger.info(f"VideoService: sent {count} video(s) to chat {chat_id}")

        return count

    # ── Funnel step helpers ──────────────────────────────────────────────

    async def send_education_videos_step2(
        self, bot: Bot, chat_id: int, reply_markup=None
    ) -> None:
        """Send 3 education videos for funnel step 2."""
        videos = [
            ("education/nuzum.mp4", "🎬 Профессор Нузум (США) — дефицит йода у 97% людей"),
            ("education/john_gray.mp4", "🎬 Профессор Джон Грей — подмена йода в гормонах Т3 и Т4"),
            ("education/brownstein.mp4", "🎬 Профессор Браунштейн — развитие заболеваний"),
        ]
        for i, (key, caption) in enumerate(videos):
            rm = reply_markup if i == len(videos) - 1 else None
            await self.send_video(bot, chat_id, key, caption=caption, reply_markup=rm)

    async def send_education_videos_step4(
        self, bot: Bot, chat_id: int, reply_markup=None
    ) -> None:
        """Send 2 education videos for funnel step 4 (presentation)."""
        videos = [
            ("education/dosage.mp4", "🎬 Как принимать Бальзам Возрождение — инструкция"),
            ("education/flechas.mp4", "🎬 Профессор Флечас — как работает йод в больших дозах"),
        ]
        for i, (key, caption) in enumerate(videos):
            rm = reply_markup if i == len(videos) - 1 else None
            await self.send_video(bot, chat_id, key, caption=caption, reply_markup=rm)

    def has_video(self, video_key: str) -> bool:
        """Check if video file exists."""
        return (self._videos_dir / video_key).exists()
