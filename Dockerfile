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

# Deno is the JS runtime yt-dlp needs for YouTube; without it some videos come back "not available"
ARG DENO_VERSION=2.9.7
ADD --checksum=sha256:c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490 \
    https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip \
    /tmp/deno.zip
RUN python -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extract('deno', '/usr/local/bin')" \
    && chmod 755 /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

# Create a non-root user and persistent data directory
RUN useradd --create-home --uid 10001 lofibot \
    && mkdir -p /app/data \
    && chown -R lofibot:lofibot /app

WORKDIR /app

# Install exact pip version and strictly verify every SHA-256 package hash
COPY requirements.txt ./
RUN pip install --no-cache-dir "pip==26.2.1" \
    && pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy bot source code
COPY --chown=lofibot:lofibot src/ ./src/

USER lofibot
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.bot"]