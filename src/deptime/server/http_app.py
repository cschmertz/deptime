"""ASGI app: MCP at /mcp (stateless Streamable HTTP, spec 2026-07-28) plus a JSON API at /v1/*.

Run: `deptime serve` or `uvicorn --factory deptime.server.http_app:create_app`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from deptime import __version__
from deptime.config import Settings, load_coverage
from deptime.index.distribution import start_background_refresh
from deptime.index.store import IndexStore
from deptime.models import SymbolQuery
from deptime.query import DepTime
from deptime.server.mcp_app import build_mcp
from deptime.server.metering import (
    FREE_TOOLS,
    InMemoryQuotaStore,
    MeteringMiddleware,
    identify,
    quota_message,
)


def load_deptime(settings: Settings) -> DepTime:
    coverage = load_coverage(settings.coverage_file)
    store = IndexStore.open(settings.index_path) if settings.index_path.exists() else None
    return DepTime(store, coverage, settings.index_path)


def _dispatch(dt: DepTime, tool: str, body: dict[str, Any]) -> BaseModel:
    if tool == "explain_error":
        return dt.explain_error(body["traceback"], body.get("installed_versions"), body.get("pip_freeze"))
    if tool == "symbol_history":
        return dt.symbol_history(body["package"], body["path"])
    if tool == "check_symbols":
        return dt.check_symbols(
            body["package"], body["version"], [SymbolQuery.model_validate(s) for s in body["symbols"]]
        )
    if tool == "diff_versions":
        return dt.diff_versions(
            body["package"],
            body["from_version"],
            body.get("to_version"),
            body.get("used_symbols"),
            bool(body.get("include_added", False)),
            int(body.get("limit", 200)),
        )
    if tool == "coverage":
        return dt.coverage_report()
    raise KeyError(tool)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    dt = load_deptime(settings)
    start_background_refresh(dt, settings)  # downloads/refreshes the index without blocking start-up
    quota = InMemoryQuotaStore(settings)  # anonymous rate limits for a public instance (DT-HOST-03)
    mcp = build_mcp(dt, middleware=[MeteringMiddleware(quota, settings)], website_url=settings.public_url)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "index_loaded": dt.store is not None,
                "index_built_at": dt.store.built_at() if dt.store else None,
            }
        )

    @mcp.custom_route("/v1/{tool}", methods=["GET", "POST"])
    async def rest(request: Request) -> JSONResponse:
        tool = request.path_params["tool"].replace("-", "_")
        if tool not in FREE_TOOLS:
            caller = identify(request.headers, request.client.host if request.client else None, settings)
            decision = quota.consume(caller)
            if not decision.allowed:
                return JSONResponse(
                    {"error": "quota_exceeded", "message": quota_message(caller, decision, settings)},
                    status_code=429,
                )
        body: dict[str, Any] = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "invalid_json"}, status_code=400)
        try:
            result = _dispatch(dt, tool, body)
        except KeyError as exc:
            return JSONResponse({"error": "bad_request", "missing_or_unknown": str(exc)}, status_code=400)
        except ValidationError as exc:
            return JSONResponse({"error": "bad_request", "detail": exc.errors()}, status_code=400)
        return JSONResponse(result.model_dump(mode="json"))

    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.host,
    )
