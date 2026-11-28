# Optional hosted mode (docs/05-self-hosting.md). Local use needs none of this: `uvx deptime mcp`.
# The index is not baked in: `deptime serve` downloads the published index into $DEPTIME_HOME in the background
# at start-up (GitHub Releases) and refreshes it daily. Mount a volume at /data to keep it across restarts.
# Both base images are pinned by digest; Dependabot moves the pins weekly (.github/dependabot.yml).
FROM ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 AS uv

FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

# Behind a proxy that appends X-Forwarded-For (or sets a client-IP header), also set DEPTIME_TRUST_PROXY_HEADERS=true
# (fly.toml does): without one, a client could send any address in that header and dodge the per-IP rate limits.
ENV DEPTIME_HOST=0.0.0.0 \
    DEPTIME_PORT=8080 \
    DEPTIME_HOME=/data

RUN mkdir -p /data && chown nobody /data
EXPOSE 8080
USER nobody
CMD ["/app/.venv/bin/deptime", "serve"]
