# Optional hosted mode (docs/05-hosting-and-sustainability.md). Local use needs none of this: `uvx deptime mcp`.
# The index is not baked in: `deptime serve` downloads the published index into $DEPTIME_HOME in the background
# at start-up (GitHub Releases) and refreshes it daily. Mount a volume at /data to keep it across restarts.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

ENV DEPTIME_HOST=0.0.0.0 \
    DEPTIME_PORT=8080 \
    DEPTIME_HOME=/data \
    DEPTIME_TRUST_PROXY_HEADERS=true

RUN mkdir -p /data && chown nobody /data
EXPOSE 8080
USER nobody
CMD ["/app/.venv/bin/deptime", "serve"]
