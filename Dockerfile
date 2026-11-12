# syntax=docker/dockerfile:1

# ---- build ----
FROM ghcr.io/astral-sh/uv:0.13-python3.13-trixie-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Dependencies first so they are cached across source changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime ----
FROM python:3.13-slim-trixie AS runtime
# year.month.sequence, set by the image workflow; /health reports it.
ARG ALMENA_VERSION
RUN useradd --system --uid 10001 --no-create-home agent
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
USER agent

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ALMENA_VERSION=$ALMENA_VERSION \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8100 \
    AGENT_ENVIRONMENT=production
EXPOSE 8100

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=2)"]

CMD ["almena-agent"]
