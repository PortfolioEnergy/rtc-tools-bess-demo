# syntax=docker/dockerfile:1.7
# ── Stage 1: Install dependencies ────────────────────────────────────────────
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

# ── Wheel export (CI compatibility placeholder) ───────────────────────────────
FROM scratch AS wheel-export

# ── Stage 2: SonarQube analysis ──────────────────────────────────────────────
FROM sonarsource/sonar-scanner-cli:latest AS sonarqube

ARG SONAR_HOST
ARG SONAR_BRANCH
ARG SONAR_TOKEN
ARG SONAR_PROJECT_KEY
ARG COVERAGE_REPORT=coverage.xml

WORKDIR /usr/src

COPY . .
COPY ${COVERAGE_REPORT} .

RUN sonar-scanner \
    -Dsonar.host.url=${SONAR_HOST} \
    -Dsonar.token=${SONAR_TOKEN} \
    -Dsonar.projectKey=${SONAR_PROJECT_KEY} \
    -Dsonar.branch.name=${SONAR_BRANCH} \
    -Dsonar.sources=. \
    -Dsonar.python.coverage.reportPaths=${COVERAGE_REPORT}

# ── Stage 3: Final image ──────────────────────────────────────────────────────
FROM base AS final

ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8010

CMD ["uv", "run", "bess-service"]
