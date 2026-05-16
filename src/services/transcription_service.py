"""Audio transcription via OpenAI Whisper API."""
from __future__ import annotations

import tempfile
from pathlib import Path

import openai
from loguru import logger

from src.config import Settings


class TranscriptionService:
    """Transcribes audio files using OpenAI Whisper."""

    def __init__(self, settings: Settings) -> None:
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    async def transcribe_file(self, file_path: str | Path) -> str | None:
        """Transcribe an audio file on disk. Returns text or None on error."""
        try:
            with open(file_path, "rb") as f:
                response = await self._client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="ru",
                )
            text = response.text.strip()
            logger.info(f"Transcribed audio ({file_path}): {text[:80]!r}")
            return text if text else None
        except Exception as exc:
            logger.error(f"Transcription failed for {file_path}: {exc}")
            return None

    async def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.ogg") -> str | None:
        """Transcribe audio from bytes. Returns text or None on error."""
        try:
            with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=True) as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
                return await self.transcribe_file(tmp.name)
        except Exception as exc:
            logger.error(f"Transcription from bytes failed: {exc}")
            return None
