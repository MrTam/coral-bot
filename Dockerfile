FROM python:3.12.10-alpine3.21

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy application code
COPY src/ src/

# Create non-root user and set up data directory
RUN adduser -D -h /app coral && \
    mkdir -p /data && \
    chown coral:coral /data

# Token and user data is persisted on a mounted volume
VOLUME /data

ENV MCP_TRANSPORT=streamable-http
EXPOSE 8080

USER coral

CMD ["uv", "run", "--no-dev", "python", "-m", "coral_bot.server"]
