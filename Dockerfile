# syntax=docker/dockerfile:1
FROM python:3.13-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
# Workspace member metadata only; the client itself is a dev dependency.
COPY clients/python/pyproject.toml ./clients/python/
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-trixie
RUN useradd --system --uid 10001 --no-create-home gateway
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ARG APP_REVISION=unknown
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_REVISION=${APP_REVISION}
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["uvicorn", "llm_gateway.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
