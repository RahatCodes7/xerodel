# syntax=docker/dockerfile:1

# ---------- Build dependencies into wheels ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------- Runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    PROOF_DIR=/app/data/proofs

# gosu lets the entrypoint fix volume permissions as root and then run the
# app as an unprivileged user. curl is kept for healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

COPY . .

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/data/proofs \
 && chown -R app:app /app/data \
 && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/healthz', timeout=5).read()" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
