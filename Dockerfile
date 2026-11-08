# API and web UI. The same image runs the CLI: docker run sloforge validate /app/examples/slos
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app /app/.venv /app/.venv
COPY --chown=app examples ./examples
# `sloforge generate` writes to /app/out by default inside the container.
RUN mkdir -p /app/out && chown app:app /app/out
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SLOFORGE_SPECS=/app/examples/slos \
    SLOFORGE_POLICY=/app/examples/policy.yaml
USER app
EXPOSE 8835
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["sloforge", "healthcheck"]
ENTRYPOINT ["sloforge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8835"]
