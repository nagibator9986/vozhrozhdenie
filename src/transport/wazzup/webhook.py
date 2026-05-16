from __future__ import annotations

import asyncio
import hmac
import html
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp
import certifi
from aiohttp import web
from loguru import logger

if TYPE_CHECKING:
    from src.config import Settings
    from src.flow.controller import FlowController
    from src.services.transcription_service import TranscriptionService
    from src.transport.base import IMessenger


# Path-traversal defense: article IDs are alphanumeric/underscore/dash only.
# Anything else is rejected before it can reach the articles registry.
_ARTICLE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# Per-article HTML body size cap (mobile readers choke on full books).
_ARTICLE_HTML_MAX_CHARS = 60_000

# Per-message size caps (DoS defense).
_MAX_AUDIO_SIZE = 20_000_000   # 20 MB
_MAX_IMAGE_SIZE = 10_000_000   # 10 MB
_MAX_DOC_SIZE = 20_000_000     # 20 MB

# Dedup TTL: how long we remember processed message IDs.
_DEDUP_TTL = 300

# Throttle: minimum seconds between accepted messages per chat_id.
_THROTTLE_SECONDS = 1.5
_THROTTLE_CLEANUP_EVERY = 200
_STALE_THROTTLE_TTL = 3600

# Cap PDF/DOCX text extraction so the LLM context stays bounded.
_DOC_TEXT_MAX_CHARS = 15_000


def _is_safe_media_url(url: str) -> bool:
    """SSRF guard for media downloads triggered by webhook payloads.

    Wazzup sends ``contentUri`` values to be fetched server-side. If the
    webhook auth ever leaks, an attacker who can post payloads could point
    these at internal services (``http://localhost``, AWS metadata at
    ``169.254.169.254``, private RFC1918/loopback ranges) to scan the host.

    Allow only ``http``/``https`` URLs whose resolved host is a public
    routable IP. Returns False (and the caller skips the fetch) on anything
    else.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        # getaddrinfo is sync — callers already run this in async contexts;
        # the lookup is fast enough (cached locally) to be acceptable inline.
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


class WazzupWebhookServer:
    """aiohttp webhook server for receiving Wazzup messages.

    Security:
      * If ``settings.wazzup_webhook_secret`` is non-empty, every incoming
        webhook request must present it via either the ``Authorization: Bearer
        <secret>`` header or the ``?token=<secret>`` query parameter. Validation
        uses constant-time comparison (``hmac.compare_digest``).
      * If the secret is empty, the server still starts but logs a loud
        warning — useful for local development, never for production.
      * The ``/articles/{article_id}`` route validates the article ID against
        a strict regex before touching the registry (defense-in-depth — the
        underlying lookup is a dict, but rejecting garbage early avoids log
        spam from random web scanners and forbids any future regression).
    """

    def __init__(
        self,
        flow_controller: "FlowController",
        messenger: "IMessenger",
        settings: "Settings",
        transcription_service: "TranscriptionService | None" = None,
    ) -> None:
        self._flow = flow_controller
        self._messenger = messenger
        self._settings = settings
        self._transcription = transcription_service
        self._webhook_secret = (settings.wazzup_webhook_secret or "").strip()
        self._idle_reminder = None  # set by main.py via set_idle_reminder()
        # Shared HTTP client for media downloads. Creating a fresh
        # aiohttp.ClientSession per request (the previous behaviour) means a
        # new TCP+TLS handshake to the same Wazzup CDN for every voice
        # message, image and document — under burst load this becomes the
        # dominant latency cost and exhausts ephemeral source ports. One
        # session with a bounded connector and keep-alive is the standard
        # fix. The session is created lazily on first use because the loop
        # must already be running.
        self._http_session: aiohttp.ClientSession | None = None
        # Cap incoming webhook body size. Wazzup posts JSON of ~KB; anything
        # bigger is a misconfigured client or an attacker trying to fill
        # memory. ``client_max_size`` bounds the per-request body buffer.
        self._app = web.Application(client_max_size=settings.webhook_max_body_size)
        self._app.router.add_post(
            settings.wazzup_webhook_path, self._handle_webhook
        )
        # Liveness/readiness endpoint — used by Docker HEALTHCHECK and uptime
        # monitors. Returns 200 OK with a tiny JSON payload, no auth needed.
        self._app.router.add_get("/health", self._handle_health)
        # Serve video files for WhatsApp contentUri
        self._app.router.add_static(
            "/media/videos/", path="data/videos/", show_index=False
        )
        # Serve screening quiz HTML pages (from Flask templates directory,
        # no templating — static HTML so direct file serving is fine).
        self._app.router.add_get("/screening/", self._serve_screening)
        self._app.router.add_get("/screening", self._serve_screening)
        self._app.router.add_get("/screening/capillary", self._serve_capillary)
        # Screening static assets (CSS/JS/images) if any
        _screening_static = os.path.abspath("screening/static")
        if os.path.isdir(_screening_static):
            self._app.router.add_static(
                "/screening/static/", path=_screening_static, show_index=False
            )
        # Self-hosted article pages (replacement for telegra.ph which is
        # blocked in Russia/Kazakhstan). See _serve_article below.
        self._app.router.add_get("/articles/{article_id}", self._serve_article)
        # Deduplication: track recent message IDs
        self._processed: dict[str, float] = {}

        # Rate limiting: per-chat_id minimum interval between processed messages
        self._last_seen: dict[str, float] = {}
        self._throttle_counter = 0

        if not self._webhook_secret:
            logger.warning(
                "Wazzup webhook is running WITHOUT an auth secret — anyone "
                "can POST to {} and impersonate Wazzup. Set "
                "WAZZUP_WEBHOOK_SECRET in .env for production.",
                settings.wazzup_webhook_path,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Auth
    # ──────────────────────────────────────────────────────────────────────

    def _check_auth(self, request: web.Request) -> bool:
        """Return True if the request presents the expected secret.

        Accepts either of:
          * ``Authorization: Bearer <secret>``
          * ``?token=<secret>`` query parameter (handy when Wazzup admin
            panel only lets you paste a webhook URL, no headers).

        Uses ``hmac.compare_digest`` to avoid timing oracles.
        """
        if not self._webhook_secret:
            return True  # explicit dev mode — already warned at startup

        provided = ""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer "):].strip()
        if not provided:
            provided = request.query.get("token", "").strip()

        if not provided:
            return False
        return hmac.compare_digest(provided, self._webhook_secret)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    # ──────────────────────────────────────────────────────────────────────
    # Static / article routes
    # ──────────────────────────────────────────────────────────────────────

    async def _serve_screening(self, request: web.Request) -> web.Response:
        """Return the main 19-symptom screening quiz HTML."""
        return await self._serve_html_file("screening/templates/screening.html")

    async def _serve_capillary(self, request: web.Request) -> web.Response:
        """Return the 15-sign capillary test HTML."""
        return await self._serve_html_file("screening/templates/capillary.html")

    async def _serve_html_file(self, path: str) -> web.Response:
        """Serve a local static HTML file (no templating)."""
        if not os.path.isfile(path):
            return web.Response(status=404, text="Not found")
        # Read off the event loop — HTML files are small but sync I/O still blocks
        body = await asyncio.to_thread(lambda: Path(path).read_bytes())
        return web.Response(
            body=body,
            content_type="text/html",
            charset="utf-8",
        )

    async def _serve_article(self, request: web.Request) -> web.Response:
        """Render a knowledge-base article as a readable HTML page.

        Replaces telegra.ph URLs which are blocked in Russia/Kazakhstan.
        Each article is stored as plain .txt in knowledge_base/articles/.
        This handler reads the txt and wraps it in a minimal, readable
        mobile-friendly HTML template.
        """
        article_id = request.match_info["article_id"]
        # Defense-in-depth: reject anything that doesn't look like an article
        # ID before it can reach a dict lookup or filesystem.
        if not _ARTICLE_ID_RE.match(article_id):
            return web.Response(status=400, text="Invalid article id")

        articles_service = getattr(self._flow, "articles", None) or getattr(
            self._flow, "_articles", None
        )
        if articles_service is None:
            return web.Response(status=503, text="Articles unavailable")
        art = articles_service.get_by_id(article_id)
        if art is None:
            return web.Response(status=404, text="Article not found")

        text = art.read_text()
        if not text:
            return web.Response(status=404, text="Article empty")

        truncated = len(text) > _ARTICLE_HTML_MAX_CHARS
        if truncated:
            text = text[:_ARTICLE_HTML_MAX_CHARS]

        # Convert plain paragraphs to HTML (simple: blank lines → <p>)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        body_html = "\n".join(
            f"<p>{html.escape(p)}</p>" for p in paragraphs
        )
        if truncated:
            body_html += "\n<p><em>— Текст сокращён для мобильного просмотра —</em></p>"
        title = html.escape(art.title)
        description = html.escape(art.description)

        html_doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Центр академика Турлубекова</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 20px 18px 60px;
    color: #1d1d1f;
    line-height: 1.65;
    font-size: 17px;
    background: #fff;
  }}
  h1 {{
    font-size: 26px;
    color: #0a4b3e;
    margin: 0 0 8px;
    font-weight: 700;
  }}
  .meta {{
    color: #86868b;
    font-size: 13px;
    margin: 0 0 24px;
    border-bottom: 1px solid #e5e5ea;
    padding-bottom: 16px;
  }}
  p {{ margin: 0 0 16px; }}
  .footer {{
    margin-top: 40px;
    padding-top: 20px;
    border-top: 1px solid #e5e5ea;
    color: #86868b;
    font-size: 14px;
    text-align: center;
  }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">{description}</p>
{body_html}
<div class="footer">
  Центр академика Турлубекова — 25 лет йодотерапии, 500 000+ клиентов
</div>
</body>
</html>"""

        return web.Response(
            body=html_doc.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    # ──────────────────────────────────────────────────────────────────────
    # Main webhook
    # ──────────────────────────────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            # Don't reveal whether the secret is missing or wrong.
            logger.warning(
                "Wazzup webhook: rejected unauthenticated request from {}",
                request.remote,
            )
            return web.Response(status=401, text="Unauthorized")

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="Bad JSON")

        # Wazzup sends {"test": true} to verify webhook
        if payload.get("test"):
            logger.info("Wazzup webhook test received — OK")
            return web.Response(status=200, text="OK")

        messages = payload.get("messages", [])
        for msg in messages:
            # Skip echo (our own outbound messages)
            if msg.get("isEcho"):
                continue

            message_id = msg.get("messageId", "")

            # Deduplication
            self._cleanup_dedup()
            if message_id in self._processed:
                continue
            self._processed[message_id] = time.time()

            chat_id = msg.get("chatId", "")
            msg_type = msg.get("type", "text")
            text = msg.get("text", "").strip()
            content_uri = msg.get("contentUri", "")

            if not chat_id:
                continue

            # Rate limit: skip messages coming faster than throttle window
            if self._is_throttled(chat_id):
                logger.debug(f"Throttled message from {chat_id}")
                continue

            user_id = f"wa:{chat_id}"

            try:
                has_media = False
                image_data: bytes | None = None
                image_media_type: str = "image/jpeg"
                document_text: str | None = None

                # Handle audio/voice messages — transcribe first
                if msg_type in ("audio", "voice", "ptt") and content_uri:
                    text = await self._transcribe_audio(content_uri, user_id, chat_id)
                    if not text:
                        continue
                # Images — download for AI vision analysis
                elif msg_type == "image" and content_uri:
                    image_data, image_media_type = await self._download_image(content_uri)
                    text = text or ""
                    if image_data is None:
                        # Download failed — treat as unsupported media so controller
                        # sends the "пришлите фото или текст" fallback message
                        has_media = True
                # Documents — try to extract text (PDF/DOCX)
                elif msg_type == "document" and content_uri:
                    document_text = await self._extract_document_text(content_uri)
                    text = text or ""
                    if document_text is None:
                        has_media = True  # unsupported format — fallback
                # Video — just flag as media (no vision on video)
                elif msg_type == "video" and content_uri:
                    has_media = True
                    text = text or ""
                # Location/contact → treat as media
                elif msg_type in ("location", "contact", "vcard"):
                    has_media = True
                    text = text or ""
                # Sticker / reaction → skip silently
                elif msg_type in ("sticker", "reaction"):
                    continue
                elif msg_type == "text" and not text:
                    continue

                await self._messenger.send_typing(chat_id)

                responses = await self._flow.handle_whatsapp_message(
                    user_id=user_id,
                    chat_id=chat_id,
                    text=text,
                    has_media=has_media,
                    image_data=image_data,
                    image_media_type=image_media_type,
                    document_text=document_text,
                )
                if responses:
                    await self._messenger.send_messages(chat_id, responses)
            except Exception as exc:
                logger.exception(f"Webhook handler error for {user_id}: {exc}")

        return web.Response(status=200, text="OK")

    # ──────────────────────────────────────────────────────────────────────
    # Media handlers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        return ssl.create_default_context(cafile=certifi.where())

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session, creating it on first use.

        Re-using one session across all media downloads keeps TCP+TLS
        connections alive between requests to the same Wazzup CDN, which
        is the difference between ~300 ms per download (fresh handshake)
        and ~30 ms (warm pool). Under 50 concurrent voice messages the
        savings compound dramatically.
        """
        if self._http_session is None or self._http_session.closed:
            connector = aiohttp.TCPConnector(
                ssl=self._ssl_context(),
                limit=64,              # global cap on parallel sockets
                limit_per_host=16,     # cap per CDN — most media share host
                ttl_dns_cache=300,
            )
            # Per-request timeout — 60 s for the whole transfer, 10 s for
            # connect. A frozen Wazzup CDN must not pin our workers.
            timeout = aiohttp.ClientTimeout(total=60, connect=10)
            self._http_session = aiohttp.ClientSession(
                connector=connector, timeout=timeout,
            )
        return self._http_session

    async def _transcribe_audio(self, content_uri: str, user_id: str, chat_id: str) -> str | None:
        """Download and transcribe audio from Wazzup contentUri."""
        from src.transport.base import OutgoingMessage  # local: avoids circular import

        if not self._transcription:
            await self._messenger.send_messages(chat_id, [
                OutgoingMessage(text="🎤 Голосовые сообщения пока не поддерживаются. Напишите текстом.", parse_mode=None)
            ])
            return None

        if not _is_safe_media_url(content_uri):
            logger.warning(f"Refusing unsafe audio URL for {user_id}: {content_uri[:120]}")
            return None

        logger.info(f"Transcribing audio for {user_id}: {content_uri[:80]}")

        # Notify user
        await self._messenger.send_messages(chat_id, [
            OutgoingMessage(text="🎤 Распознаю голосовое сообщение…", parse_mode=None)
        ])

        try:
            session = await self._get_http_session()
            async with session.get(content_uri) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to download audio: HTTP {resp.status}")
                    return None
                content_length = int(resp.headers.get("Content-Length", 0))
                if content_length > _MAX_AUDIO_SIZE:
                    logger.error(f"Audio too large: {content_length} bytes")
                    await self._messenger.send_messages(chat_id, [
                        OutgoingMessage(text="❌ Аудио слишком большое. Отправьте файл покороче или напишите текстом.", parse_mode=None)
                    ])
                    return None
                # Enforce the cap even if Content-Length is missing/wrong.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > _MAX_AUDIO_SIZE:
                        logger.warning(f"Audio exceeds {_MAX_AUDIO_SIZE} bytes — aborting")
                        return None
                    chunks.append(chunk)
                audio_bytes = b"".join(chunks)

            text = await self._transcription.transcribe_bytes(audio_bytes, filename="voice.ogg")
            if text:
                # Show transcription to user
                await self._messenger.send_messages(chat_id, [
                    OutgoingMessage(text=f"🎤 _{text}_", parse_mode=None)
                ])
            return text
        except Exception as exc:
            logger.error(f"Audio transcription failed for {user_id}: {exc}")
            return None

    async def _download_image(self, content_uri: str) -> tuple[bytes | None, str]:
        """Download image from Wazzup contentUri. Returns (bytes, media_type).

        Uses streaming read so the size cap is enforced even when the server
        omits Content-Length (common with CDNs).
        """
        if not _is_safe_media_url(content_uri):
            logger.warning(f"Refusing unsafe image URL: {content_uri[:120]}")
            return None, "image/jpeg"
        try:
            session = await self._get_http_session()
            async with session.get(content_uri) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to download image: HTTP {resp.status}")
                    return None, "image/jpeg"
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                # gpt-4o / Claude support: image/jpeg, image/png, image/gif, image/webp
                if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    content_type = "image/jpeg"
                # Stream with hard size cap — enforced even when Content-Length absent
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > _MAX_IMAGE_SIZE:
                        logger.warning(f"Image exceeds {_MAX_IMAGE_SIZE} bytes — aborting")
                        return None, "image/jpeg"
                    chunks.append(chunk)
                data = b"".join(chunks)
                logger.info(f"Downloaded image: {len(data)} bytes, type={content_type}")
                return data, content_type
        except Exception as exc:
            logger.error(f"Image download failed: {exc}")
            return None, "image/jpeg"

    async def _extract_document_text(self, content_uri: str) -> str | None:
        """Download document and extract text (PDF/DOCX). Returns None if unsupported.

        PDF/DOCX extraction runs in a thread pool (asyncio.to_thread) to avoid
        blocking the event loop — pdfminer can take 1-10 s on large files.
        Streaming download enforces the size cap even without Content-Length.
        """
        if not _is_safe_media_url(content_uri):
            logger.warning(f"Refusing unsafe document URL: {content_uri[:120]}")
            return None
        try:
            session = await self._get_http_session()
            async with session.get(content_uri) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to download document: HTTP {resp.status}")
                    return None
                content_type = resp.headers.get("Content-Type", "").lower()
                # Stream with hard size cap
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > _MAX_DOC_SIZE:
                        logger.warning(f"Document exceeds {_MAX_DOC_SIZE} bytes — aborting")
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)

            # Detect format from URL or Content-Type
            uri_lower = content_uri.lower()
            is_pdf = "pdf" in content_type or uri_lower.endswith(".pdf")
            is_docx = (
                "docx" in content_type
                or "wordprocessingml" in content_type
                or uri_lower.endswith(".docx")
            )

            # Run CPU-bound extraction off the event loop
            if is_pdf:
                return await asyncio.to_thread(self._extract_pdf_text, data)
            elif is_docx:
                return await asyncio.to_thread(self._extract_docx_text, data)
            else:
                # Unknown type — try PDF then DOCX
                text = await asyncio.to_thread(self._extract_pdf_text, data)
                if text:
                    return text
                return await asyncio.to_thread(self._extract_docx_text, data)
        except Exception as exc:
            logger.error(f"Document extraction failed: {exc}")
            return None

    @staticmethod
    def _extract_pdf_text(data: bytes) -> str | None:
        """Extract text from PDF bytes using pdfminer."""
        try:
            from pdfminer.high_level import extract_text_to_fp
            from pdfminer.layout import LAParams
            buf = io.StringIO()
            extract_text_to_fp(io.BytesIO(data), buf, laparams=LAParams(), output_type="text")
            text = buf.getvalue().strip()
            return text[:_DOC_TEXT_MAX_CHARS] if text else None
        except Exception as exc:
            logger.debug(f"PDF extraction failed: {exc}")
            return None

    @staticmethod
    def _extract_docx_text(data: bytes) -> str | None:
        """Extract text from DOCX bytes using python-docx."""
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(io.BytesIO(data))
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            text = "\n".join(paragraphs)
            return text[:_DOC_TEXT_MAX_CHARS] if text else None
        except Exception as exc:
            logger.debug(f"DOCX extraction failed: {exc}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Dedup / throttle bookkeeping
    # ──────────────────────────────────────────────────────────────────────

    def _cleanup_dedup(self) -> None:
        now = time.time()
        expired = [k for k, v in self._processed.items() if now - v > _DEDUP_TTL]
        for k in expired:
            del self._processed[k]

    def _is_throttled(self, chat_id: str) -> bool:
        """Per-chat_id throttle: reject if last message was < THROTTLE_SECONDS ago."""
        now = time.time()
        last = self._last_seen.get(chat_id)
        if last is not None and (now - last) < _THROTTLE_SECONDS:
            return True
        self._last_seen[chat_id] = now

        # Periodic cleanup to prevent unbounded memory growth
        self._throttle_counter += 1
        if self._throttle_counter >= _THROTTLE_CLEANUP_EVERY:
            self._throttle_counter = 0
            cutoff = now - _STALE_THROTTLE_TTL
            expired = [k for k, v in self._last_seen.items() if v < cutoff]
            for k in expired:
                del self._last_seen[k]
        return False

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    def set_idle_reminder(self, service) -> None:
        """Attach an IdleReminderService to be started alongside the webhook."""
        self._idle_reminder = service

    async def start(self, port: int = 8080) -> None:
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        logger.info(f"Wazzup webhook server starting on port {port}")
        await site.start()
        # Start background idle-reminder scanner if configured
        if self._idle_reminder is not None:
            self._idle_reminder.start()
        # Keep running (called inside asyncio.gather). Sleep on a stop event
        # so shutdown stays clean instead of looping on sleep(3600).
        self._stop_event = asyncio.Event()
        try:
            await self._stop_event.wait()
        finally:
            # Close the shared media-download session before tearing down
            # the runner so we don't leak the connector pool.
            if self._http_session is not None and not self._http_session.closed:
                await self._http_session.close()
            await runner.cleanup()

    async def stop(self) -> None:
        logger.info("Wazzup webhook server stopping")
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
