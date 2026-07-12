# kassensturz Web — schlankes Read-only-Frontend auf die lokale SQLite.
#
#   docker build -t kassensturz .
#   docker run -p 8420:8420 \
#     -v ~/.kassensturz:/data \
#     -e KASSENSTURZ_WEB_PASSWORD=geheim \
#     kassensturz
#
# Die DB entsteht weiterhin per CLI/Sync auf dem Host (Banking-Logins
# brauchen Apps/TANs — das gehört nicht in einen Container).

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV KASSENSTURZ_DB=/data/kassensturz.db \
    KASSENSTURZ_HOME=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME /data
EXPOSE 8420

CMD ["kassensturz", "web", "--host", "0.0.0.0", "--port", "8420"]
