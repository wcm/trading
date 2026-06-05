FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY config ./config
RUN mkdir -p data logs

CMD ["uv", "run", "--no-sync", "trading-bot", "schedule-local", "--send-discord", "--send-cycle-discord", "--cycle-summary-only", "--json-output-dir", "data/scheduler_cycles", "--submit-paper", "--submit-paper-close"]
