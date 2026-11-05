# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UREP_STATE_DIR=/var/lib/urep \
    UREP_DATA_DIR=/urep

VOLUME ["/urep", "/var/lib/urep"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD urep status --json >/dev/null || exit 1

ENTRYPOINT ["urep"]
CMD ["daemon"]
