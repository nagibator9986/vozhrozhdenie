from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from loguru import logger
from telegraph import Telegraph

from src.services.articles_service import Article

# Maximum characters of article text to publish (Telegraph page size ~30 KB safe limit)
_MAX_CHARS = 28_000
# Cache file: maps article_id → telegraph URL. Lives under STATE_DIR so the
# Telegraph publish history follows the persistent volume on prod.
_CACHE_FILE = Path(os.environ.get("STATE_DIR", "data")) / "telegraph_cache.json"


def _text_to_nodes(text: str) -> list[dict]:
    """
    Convert plain .txt article content to Telegraph node format.
    Paragraphs separated by blank lines; long lines are wrapped.
    """
    nodes: list[dict] = []
    paragraphs = re.split(r"\n{2,}", text.strip())

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Detect headings: all-caps line, or short line ending without punctuation
        lines = para.splitlines()
        if (
            len(lines) == 1
            and len(para) < 120
            and (para.isupper() or not para[-1] in ".,:;?!")
            and not para[0].isdigit()
        ):
            nodes.append({"tag": "h4", "children": [para]})
        else:
            # Join wrapped lines and make a paragraph
            content = " ".join(l.strip() for l in lines if l.strip())
            nodes.append({"tag": "p", "children": [content]})

    return nodes if nodes else [{"tag": "p", "children": [text[:500]]}]


class TelegraphService:
    """
    Publishes articles to telegra.ph so users can read them
    inside Telegram's built-in Instant View reader.

    Pages are published once and cached in data/telegraph_cache.json.
    """

    def __init__(self, access_token: str) -> None:
        self._telegraph = Telegraph(access_token=access_token) if access_token else None
        self._cache: dict[str, str] = {}
        self._cache_loaded = False

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return self._telegraph is not None

    def get_url(self, article: Article) -> Optional[str]:
        """Return cached Telegraph URL for an article (None if not yet published)."""
        self._load_cache()
        return self._cache.get(article.id)

    def prime_cache(self) -> int:
        """Eagerly load the on-disk cache and return its size.

        Public entry point so that startup code can warm the cache without
        reaching into the underscore-prefixed loader.
        """
        self._load_cache()
        return len(self._cache)

    def cache_size(self) -> int:
        """Number of (article_id, telegraph_url) pairs currently cached."""
        return len(self._cache)

    def publish(self, article: Article) -> Optional[str]:
        """
        Publish article to Telegraph and return the URL.
        Returns cached URL if already published.
        """
        if not self._telegraph:
            return None

        self._load_cache()
        if article.id in self._cache:
            return self._cache[article.id]

        text = article.read_text()
        if not text:
            return None

        try:
            nodes = _text_to_nodes(text[:_MAX_CHARS])
            lang_note = " (англ.)" if article.language == "en" else ""
            result = self._telegraph.create_page(
                title=f"{article.title}{lang_note}",
                author_name="Бальзам Возрождение Plus",
                content=nodes,
            )
            url: str = result["url"]
            self._cache[article.id] = url
            self._save_cache()
            logger.info(f"Published to Telegraph: {article.id} → {url}")
            return url
        except Exception as exc:
            logger.error(f"Telegraph publish failed for {article.id}: {exc}")
            return None

    def publish_all(self, articles: list[Article]) -> int:
        """
        Publish all articles that haven't been published yet.
        Adds a delay between requests to respect Telegraph rate limits.
        Returns count of newly published.
        """
        if not self._telegraph:
            return 0
        self._load_cache()
        published = 0
        for art in articles:
            if art.id not in self._cache:
                if self.publish(art):
                    published += 1
                    time.sleep(1.2)  # Telegraph flood control: max ~1 req/sec
        return published

    # ──────────────────────────────────────────────────────────────────────
    # Cache helpers
    # ──────────────────────────────────────────────────────────────────────

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _CACHE_FILE.exists():
            try:
                self._cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                logger.debug(f"Telegraph cache loaded: {len(self._cache)} entries")
            except Exception as exc:
                logger.warning(f"Failed to load Telegraph cache: {exc}")
                self._cache = {}
        self._cache_loaded = True

    def _save_cache(self) -> None:
        try:
            _CACHE_FILE.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(f"Failed to save Telegraph cache: {exc}")
