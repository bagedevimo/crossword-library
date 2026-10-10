FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/tmp/xdg

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app
USER app

CMD ["crossword-library", "serve"]
