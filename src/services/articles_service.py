from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from loguru import logger

from src.config import Settings

_ARTICLES_PER_PAGE = 8


class Article:
    """Represents a single article in the knowledge library."""

    __slots__ = (
        "id", "title", "filename", "category", "language",
        "description", "path", "original_path",
    )

    def __init__(self, data: dict, articles_dir: Path) -> None:
        self.id: str = data["id"]
        self.title: str = data["title"]
        self.filename: str = data["filename"]
        self.category: str = data["category"]
        self.language: str = data.get("language", "ru")
        self.description: str = data.get("description", "")
        self.path: Path = articles_dir / self.filename

        # Original file (PDF/DOCX) path relative to project root
        orig = data.get("original_file")
        if orig:
            project_root = articles_dir.parent.parent
            self.original_path: Optional[Path] = project_root / orig
        else:
            self.original_path = None

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def lang_label(self) -> str:
        return "🇷🇺" if self.language == "ru" else "🇬🇧"

    def read_text(self) -> str:
        """Return full article text from .txt file (empty string if missing)."""
        if not self.path.exists():
            logger.warning(f"Article .txt file not found: {self.path}")
            return ""
        return self.path.read_text(encoding="utf-8", errors="replace")

    def read_original_bytes(self) -> Optional[tuple[bytes, str]]:
        """
        Return (raw_bytes, file_extension) of the original PDF/DOCX file.
        Returns None if no original file is registered or the file is missing.
        """
        if not self.original_path:
            return None
        if not self.original_path.exists():
            logger.warning(f"Original file not found: {self.original_path}")
            return None
        try:
            return self.original_path.read_bytes(), self.original_path.suffix
        except Exception as exc:
            logger.error(f"Failed to read original file {self.original_path}: {exc}")
            return None

    def preview(self, chars: int = 300) -> str:
        """Return a short preview of the article content."""
        text = self.read_text()
        if not text:
            return self.description
        clean = re.sub(r"\s+", " ", text[:chars * 2]).strip()
        return clean[:chars] + "…" if len(clean) > chars else clean


class ArticlesService:
    """
    Manages the scientific article library.

    Features:
    - Load article registry from ARTICLES_INDEX.json
    - List articles by category with pagination
    - Full-text search with Russian inflection handling
    - Return full article text / original file bytes for sending to users
    - Build a compact catalogue for injection into the AI system prompt
    """

    CATEGORIES_ORDER = [
        "мастопатия",
        "галогены",
        "йодотерапия",
        "щитовидная",
        "другие_болезни",
        "наука_абрахам",
    ]

    def __init__(self, settings: Settings) -> None:
        self._articles_dir = Path(settings.knowledge_base_path) / "articles"
        self._index_path = self._articles_dir / "ARTICLES_INDEX.json"
        self._articles: list[Article] = []
        self._by_id: dict[str, Article] = {}
        self._categories: dict[str, str] = {}
        self._loaded = False

    # ──────────────────────────────────────────────────────────────────────
    # Initialisation
    # ──────────────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load the article registry from ARTICLES_INDEX.json."""
        if self._loaded:
            return
        if not self._index_path.exists():
            logger.warning(f"Articles index not found: {self._index_path}")
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            self._categories = data.get("categories", {})
            for item in data.get("articles", []):
                art = Article(item, self._articles_dir)
                if art.exists:
                    self._articles.append(art)
                    self._by_id[art.id] = art
                else:
                    logger.debug(f"Article .txt file missing, skipping: {art.filename}")
            logger.info(f"ArticlesService: loaded {len(self._articles)} articles.")
            self._loaded = True
        except Exception as exc:
            logger.error(f"Failed to load articles index: {exc}")

    # ──────────────────────────────────────────────────────────────────────
    # Queries
    # ──────────────────────────────────────────────────────────────────────

    def get_by_id(self, article_id: str) -> Optional[Article]:
        return self._by_id.get(article_id)

    def all(self) -> list[Article]:
        """Return all loaded articles. Stable order (load order)."""
        return list(self._articles)

    def count(self) -> int:
        """Number of articles currently registered."""
        return len(self._articles)

    def is_loaded(self) -> bool:
        return self._loaded

    def list_by_category(self, category: str) -> list[Article]:
        return [a for a in self._articles if a.category == category]

    def list_by_category_paged(
        self, category: str, page: int, per_page: int = _ARTICLES_PER_PAGE
    ) -> tuple[list[Article], int]:
        """
        Return (page_articles, total_pages) for the given category and page index (0-based).
        """
        all_arts = self.list_by_category(category)
        total = len(all_arts)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        start = page * per_page
        return all_arts[start: start + per_page], total_pages

    def all_categories(self) -> list[tuple[str, str]]:
        """Return (category_key, category_label) pairs in display order."""
        result = []
        for key in self.CATEGORIES_ORDER:
            if key in self._categories and any(a.category == key for a in self._articles):
                result.append((key, self._categories[key]))
        return result

    # ──────────────────────────────────────────────────────────────────────
    # Search
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _word_matches(query_word: str, haystack: str) -> bool:
        """
        True if query_word matches any token in haystack by exact or prefix match.
        Handles Russian case inflections: "абрахама" matches "абрахам", etc.
        """
        if query_word in haystack:
            return True
        tokens = re.split(r"\W+", haystack)
        return any(
            token and (token.startswith(query_word) or query_word.startswith(token))
            for token in tokens
            if len(token) >= 3
        )

    def search(self, query: str, max_results: int = 5) -> list[Article]:
        """
        Find articles whose title or description contains any word from the query.
        Uses prefix matching to handle Russian case inflections.
        Returns up to max_results results ordered by match quality.

        Note: the main WhatsApp article-injection path uses
        match_articles_by_context() in response_enforcer.py (regex triggers),
        not this method. This search() is kept for the Telegram article
        browser and LLM catalogue.
        """
        query_lower = query.lower()
        words = [w for w in re.split(r"\W+", query_lower) if len(w) >= 3]
        if not words:
            return []

        scored: list[tuple[int, Article]] = []
        for art in self._articles:
            haystack = (art.title + " " + art.description).lower()
            score = sum(1 for w in words if self._word_matches(w, haystack))
            if score:
                title_haystack = art.title.lower()
                title_score = sum(2 for w in words if self._word_matches(w, title_haystack))
                scored.append((score + title_score, art))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [a for _, a in scored[:max_results]]

    # ──────────────────────────────────────────────────────────────────────
    # System-prompt catalogue
    # ──────────────────────────────────────────────────────────────────────

    def build_catalogue_for_prompt(self) -> str:
        """
        Return a compact article catalogue to inject into the AI system prompt
        so the model knows which articles are available and can reference them.
        """
        if not self._articles:
            return ""

        lines = ["ДОСТУПНЫЕ НАУЧНЫЕ СТАТЬИ И МАТЕРИАЛЫ (можно предложить пользователю):"]
        for cat_key, cat_label in self.all_categories():
            cat_articles = self.list_by_category(cat_key)
            if not cat_articles:
                continue
            lines.append(f"\n📚 {cat_label}:")
            for art in cat_articles:
                lang = " (англ.)" if art.language == "en" else ""
                lines.append(f"  • [{art.id}] {art.title}{lang} — {art.description}")

        lines.append(
            "\nЕсли тема разговора связана с одной из статей — упомяни её по названию "
            "и предложи: «Хотите, я пришлю вам эту статью?»"
        )
        return "\n".join(lines)
