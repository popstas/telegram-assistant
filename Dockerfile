FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .

# Runtime state (Telethon session, SQLite DB, data/config.yml) is mounted at
# /data. Symlink data -> /data so the default loader path `data/config.yml`
# (resolved relative to WORKDIR) keeps working without an env override.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && ln -s /data /app/data \
    && chown -R app:app /app /data

USER app

VOLUME ["/data"]

EXPOSE 8085

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "telegram_assistant.http_api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8085"]
