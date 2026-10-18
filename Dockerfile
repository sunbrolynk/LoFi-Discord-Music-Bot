# Pin to Python 3.12 Bookworm Slim (matches Python 3.12 hash-pinned wheels in requirements.txt)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install FFmpeg, Opus codec, and voice encryption system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libffi-dev \
    libnacl-dev \
    libsodium23 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and persistent data directory
RUN useradd --create-home --uid 10001 lofibot \
    && mkdir -p /app/data \
    && chown -R lofibot:lofibot /app

WORKDIR /app

# Install exact pip version and strictly verify every SHA-256 package hash
COPY requirements.txt ./
RUN pip install --no-cache-dir "pip==24.3.1" \
    && pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy bot source code
COPY --chown=lofibot:lofibot src/ ./src/

USER lofibot
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.bot"]