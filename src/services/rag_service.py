from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List

import aiofiles
import chromadb
from chromadb.config import Settings as ChromaSettings
from loguru import logger
from sentence_transformers import SentenceTransformer

from src.config import Settings
from src.domain.entities import Document
from src.domain.interfaces import IRAGService


class RAGService(IRAGService):
    """
    RAG service using ChromaDB for vector storage and sentence-transformers for embeddings.

    Improvements over initial version:
    - CPU-bound encode() calls are offloaded to a thread pool via asyncio.to_thread()
      so they never block the event loop.
    - File I/O uses aiofiles (async).
    - On startup the collection is checked first; documents are re-indexed only when
      the collection is empty OR when force=True is passed.
    - Text is split on paragraph/sentence boundaries to preserve semantic coherence.
    - Chunk size and overlap are driven by Settings (configurable via .env).
    """

    COLLECTION_NAME = "vozrozhdenie_knowledge"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: SentenceTransformer | None = None
        self._client: chromadb.PersistentClient | None = None
        self._collection: chromadb.Collection | None = None
        # Serializes concurrent index_documents() calls so a burst of "empty
        # collection" retrievals does not spawn N parallel reindexes (each of
        # which reads the whole knowledge base, runs the encoder, and upserts
        # to ChromaDB).
        self._index_lock = asyncio.Lock()
        # Hold a strong reference to the most recent background reindex task
        # so the asyncio runtime does not garbage-collect it mid-flight.
        self._bg_reindex: asyncio.Task | None = None
        # Bound the number of parallel embedding encodings. The
        # SentenceTransformer model is a single in-process instance backed
        # by PyTorch — running 50 concurrent encodes from a burst of
        # WhatsApp users would saturate the default thread pool (32 threads
        # on Python 3.13), thrash CPU caches, and balloon tail latency.
        # A handful of concurrent encodes is the sweet spot: throughput
        # stays high while p99 latency stays predictable.
        self._encode_semaphore = asyncio.Semaphore(
            max(1, settings.rag_max_concurrent)
        )

    # ──────────────────────────────────────────────────────────────────────
    # Lazy initialisation helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _get_model(self) -> SentenceTransformer:
        """Load the embedding model in a thread pool (first call only)."""
        if self._model is None:
            logger.info("Loading sentence-transformer model (may take a moment)…")
            self._model = await asyncio.to_thread(
                SentenceTransformer, "paraphrase-multilingual-MiniLM-L12-v2"
            )
            logger.info("Sentence-transformer model loaded.")
        return self._model

    def _get_collection(self) -> chromadb.Collection:
        """Return (or create) the ChromaDB persistent client and collection."""
        if self._collection is None:
            db_path = self._settings.chroma_db_path
            os.makedirs(db_path, exist_ok=True)
            if self._client is None:
                self._client = chromadb.PersistentClient(
                    path=db_path,
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
            self._collection = self._client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # ──────────────────────────────────────────────────────────────────────
    # Smart text chunking (paragraph-aware)
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _split_text(text: str, chunk_size: int, overlap: int) -> List[str]:
        """
        Split *text* into semantically coherent chunks.

        Strategy:
        1. Split by blank lines into paragraphs.
        2. Accumulate paragraphs until the chunk would exceed *chunk_size*.
        3. When a single paragraph is larger than *chunk_size*, fall back to
           sentence-level splitting.
        4. Each chunk carries *overlap* characters of context from the previous
           chunk to maintain continuity.
        """
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: List[str] = []
        current_parts: List[str] = []
        current_len = 0

        def _flush(parts: List[str]) -> str:
            return "\n\n".join(parts)

        for para in paragraphs:
            # If adding this paragraph stays within budget, accumulate it
            if current_len + len(para) + 2 <= chunk_size:
                current_parts.append(para)
                current_len += len(para) + 2
                continue

            # Flush what we have so far
            if current_parts:
                chunk = _flush(current_parts)
                chunks.append(chunk)
                # Carry overlap characters from the end of the previous chunk
                overlap_text = chunk[-overlap:] if overlap and len(chunk) > overlap else ""
                current_parts = [overlap_text] if overlap_text else []
                current_len = len(overlap_text)

            # If the paragraph itself is too long, split it by sentences
            if len(para) > chunk_size:
                sentences = [s.strip() for s in para.replace(".\n", ". ").split(". ") if s.strip()]
                for sentence in sentences:
                    sentence = sentence if sentence.endswith(".") else sentence + "."
                    if current_len + len(sentence) + 1 <= chunk_size:
                        current_parts.append(sentence)
                        current_len += len(sentence) + 1
                    else:
                        if current_parts:
                            chunk = _flush(current_parts)
                            chunks.append(chunk)
                            overlap_text = chunk[-overlap:] if overlap and len(chunk) > overlap else ""
                            current_parts = [overlap_text, sentence] if overlap_text else [sentence]
                            current_len = len(overlap_text) + len(sentence) + 1
                        else:
                            # Single sentence larger than chunk — store as-is
                            chunks.append(sentence)
                            current_parts = []
                            current_len = 0
            else:
                current_parts = [para]
                current_len = len(para)

        if current_parts:
            chunks.append(_flush(current_parts))

        return [c for c in chunks if c.strip()]

    # ──────────────────────────────────────────────────────────────────────
    # IRAGService implementation
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _on_bg_reindex_done(task: asyncio.Task) -> None:
        """Log any exception from the background reindex task so it isn't
        silently swallowed when the strong reference is released."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Background RAG reindex failed: {exc!r}")

    async def index_documents(self, force: bool = False) -> None:
        """
        Load .txt files from knowledge_base/, chunk them and upsert into ChromaDB.

        If the collection already has documents and *force* is False, indexing is
        skipped to avoid redundant work on every restart.

        Wrapped in a per-instance lock so a thundering herd of "empty
        collection" retrievals (which fire-and-forget a background reindex)
        does not produce N parallel index passes against the same store.
        """
        async with self._index_lock:
            await self._index_documents_locked(force=force)

    async def _index_documents_locked(self, force: bool = False) -> None:
        collection = self._get_collection()

        if not force and collection.count() > 0:
            logger.info(
                f"ChromaDB collection already contains {collection.count()} chunks — skipping re-index. "
                "Pass force=True to rebuild."
            )
            return

        kb_path = Path(self._settings.knowledge_base_path)
        if not kb_path.exists():
            logger.warning(f"Knowledge base path does not exist: {kb_path}")
            return

        # Recursively find all .txt files (includes articles/ subdirectory)
        txt_files = [
            f for f in kb_path.rglob("*.txt")
            if not f.name.startswith(".")  # skip hidden files
        ]
        if not txt_files:
            logger.warning(f"No .txt files found under {kb_path}")
            return

        logger.info(f"Found {len(txt_files)} .txt files to index.")
        chunk_size = self._settings.rag_chunk_size
        chunk_overlap = self._settings.rag_chunk_overlap

        all_chunks: List[str] = []
        all_ids: List[str] = []
        all_metadata: List[dict] = []

        for file_path in txt_files:
            # Unique ID includes relative path so same filename in different dirs doesn't clash
            relative = file_path.relative_to(kb_path)
            logger.info(f"Reading: {relative}")
            try:
                async with aiofiles.open(file_path, encoding="utf-8") as fh:
                    text = await fh.read()
            except Exception as exc:
                logger.error(f"Failed to read {file_path}: {exc}")
                continue

            chunks = self._split_text(text, chunk_size, chunk_overlap)
            logger.debug(f"  → {len(chunks)} chunks from {relative}")
            for idx, chunk in enumerate(chunks):
                all_chunks.append(chunk)
                # Use relative path as ID prefix to avoid collisions across subdirs
                safe_id = str(relative).replace("\\", "/").replace(" ", "_")
                all_ids.append(f"{safe_id}_{idx}")
                all_metadata.append({
                    "source": file_path.name,
                    "relative_path": str(relative),
                    "chunk_index": idx,
                })

        if not all_chunks:
            logger.warning("No chunks to index.")
            return

        logger.info(f"Generating embeddings for {len(all_chunks)} chunks…")
        model = await self._get_model()
        # Offload CPU-intensive encoding to a thread; the encode_semaphore
        # ensures we don't compete with concurrent query-time encodes for
        # the same single-process PyTorch model.
        async with self._encode_semaphore:
            embeddings: List[List[float]] = await asyncio.to_thread(
                lambda: model.encode(all_chunks, show_progress_bar=False).tolist()
            )

        # Upsert in batches to avoid excessive memory pressure
        batch_size = 100
        for i in range(0, len(all_chunks), batch_size):
            collection.upsert(
                ids=all_ids[i: i + batch_size],
                documents=all_chunks[i: i + batch_size],
                embeddings=embeddings[i: i + batch_size],
                metadatas=all_metadata[i: i + batch_size],
            )

        logger.info(
            f"Indexed {len(all_chunks)} chunks into ChromaDB collection '{self.COLLECTION_NAME}'."
        )

    async def retrieve(self, query: str, k: int = 3) -> List[Document]:
        """Return the top-k most semantically similar document chunks for the query."""
        collection = self._get_collection()

        count = collection.count()
        if count == 0:
            # Only spawn a background reindex if one isn't already running.
            # Keeping a strong reference on self._bg_reindex prevents the
            # asyncio loop from garbage-collecting the task mid-flight (a
            # classic "fire-and-forget" footgun that loses the work and
            # silently swallows exceptions).
            if self._bg_reindex is None or self._bg_reindex.done():
                logger.warning(
                    "ChromaDB collection is empty — triggering background indexing. "
                    "This message will be answered without RAG context."
                )
                self._bg_reindex = asyncio.create_task(
                    self.index_documents(force=True),
                    name="rag-background-reindex",
                )
                # Surface errors instead of swallowing them on GC.
                self._bg_reindex.add_done_callback(self._on_bg_reindex_done)
            else:
                logger.debug("Background reindex already in flight — skipping spawn")
            return []

        k = min(k, count)

        try:
            model = await self._get_model()
            # Bound parallel encode() calls so a burst of concurrent users
            # doesn't oversubscribe the thread pool and the single PyTorch
            # model. Each retrieval queues behind at most rag_max_concurrent
            # in-flight encodes.
            async with self._encode_semaphore:
                query_embedding: List[float] = await asyncio.to_thread(
                    lambda: model.encode([query], show_progress_bar=False).tolist()[0]
                )
            # ChromaDB's query() is a CPU-bound C call against a memory-
            # mapped HNSW index — offload to a thread so it doesn't block
            # the asyncio loop when N concurrent users retrieve at once.
            results = await asyncio.to_thread(
                lambda: collection.query(
                    query_embeddings=[query_embedding],
                    n_results=k,
                    include=["documents", "metadatas", "distances"],
                )
            )
        except Exception as exc:
            logger.error(f"ChromaDB query failed: {exc}")
            return []

        documents: List[Document] = []
        if results and results.get("documents"):
            docs = results["documents"][0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]
            for doc_text, meta, dist in zip(docs, metas, distances):
                score = float(1.0 - dist)  # cosine distance → similarity
                logger.debug(
                    f"RAG hit: source={meta.get('source', '?')} "
                    f"chunk={meta.get('chunk_index', '?')} score={score:.3f}"
                )
                documents.append(Document(content=doc_text, metadata=meta or {}, score=score))

        return documents
