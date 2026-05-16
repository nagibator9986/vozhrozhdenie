# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder
# Installs build toolchain, compiles Python wheels, and pre-downloads the
# sentence-transformer model. Everything heavy stays in this layer and is
# discarded from the final image.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a dedicated prefix so they can be copied
# wholesale into the runtime image. --prefix keeps the layout identical to
# /usr/local, which is where the runtime stage expects them.
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Pre-download the sentence-transformer model so the first container start
# doesn't have to hit Hugging Face. The model lands in a known directory we
# can copy across stages.
ENV SENTENCE_TRANSFORMERS_HOME=/models
RUN PYTHONPATH=/install/lib/python3.11/site-packages \
    python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime
# Minimal slim image, no compiler, no apt caches. Just Python + the wheels we
# built + the pre-downloaded model.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTENCE_TRANSFORMERS_HOME=/models \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1

# curl is used by the HEALTHCHECK. procps gives us `pgrep` as a fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages and pre-cached model from the builder.
COPY --from=builder /install /usr/local
COPY --from=builder /models /models

WORKDIR /app

# Copy application source last so source edits don't bust the heavy layers.
COPY . .

# Runtime directories (data is also mounted as a volume in compose).
RUN mkdir -p data/chroma_db data/videos knowledge_base/articles

# Non-root user for defense-in-depth.
RUN adduser --disabled-password --gecos "" botuser \
    && chown -R botuser:botuser /app /models
USER botuser

EXPOSE 8080

# Real healthcheck:
#  • If the Wazzup webhook server is enabled it exposes /health on $WAZZUP_WEBHOOK_PORT.
#  • Otherwise (Telegram-only mode) we fall back to verifying the python
#    process is still running. Neither check is perfect, but both are
#    strictly better than the original no-op (`python -c "sys.exit(0)"`)
#    which always passed even if the bot was a zombie.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WAZZUP_WEBHOOK_PORT:-8080}/health" >/dev/null 2>&1 \
        || pgrep -f "python.*main.py" >/dev/null \
        || exit 1

CMD ["python", "main.py"]
