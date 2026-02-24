FROM python:3.13-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group service

# Copy application code
COPY service/ service/
COPY scheduling/ scheduling/
COPY continuous_intraday/ continuous_intraday/
COPY README.md ./

# Install the project itself
RUN uv sync --frozen --group service

EXPOSE 8010

CMD ["uv", "run", "bess-service"]
