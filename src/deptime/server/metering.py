"""Caller identification and rate limits for a *hosted* DepTime instance (docs/05-hosting-and-sustainability.md).

Local stdio mode never meters. A public instance needs anonymous per-IP limits so one client can't exhaust it;
keys (`DEPTIME_DEV_API_KEYS`) let trusted users bypass them. Paid plans are parked (see BACKLOG.md "Parked").
The quota store is in-memory; a persistent one is DT-HOST-03.
"""

from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from mcp.types import CallToolResult, TextContent

from deptime.config import Settings

Plan = Literal["local", "anonymous", "free", "pro", "credits"]
FREE_TOOLS = frozenset({"coverage"})


@dataclass(frozen=True)
class Caller:
    id: str  # stable, non-reversible identifier (hashed IP or key id), safe to log
    plan: Plan


@dataclass(frozen=True)
class Decision:
    allowed: bool
    used: int
    limit: int | None
    window: str  # "day" | "month" | "none"


class QuotaStore(Protocol):
    def consume(self, caller: Caller, now: datetime | None = None) -> Decision: ...


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def identify(headers: Mapping[str, str] | None, client_host: str | None, settings: Settings) -> Caller:
    """Bearer key -> plan from the key store; otherwise anonymous by (hashed) client IP; no HTTP -> local."""
    if headers is None:
        return Caller("local", "local")
    auth = headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-api-key", "").strip()
    if key:
        plan = _dev_keys(settings).get(key)
        if plan is not None:
            return Caller(f"key:{_hash(key)}", plan)
        # Unknown keys fall through to anonymous limits rather than erroring; the quota message explains.
    ip = (_proxied_client_ip(headers, settings) if settings.trust_proxy_headers else None) or client_host
    return Caller(f"ip:{_hash(ip or 'unknown')}", "anonymous")


def _proxied_client_ip(headers: Mapping[str, str], settings: Settings) -> str | None:
    """The client address as reported by *our* proxies, never by the client.

    Proxies append the address they saw to X-Forwarded-For, so anything left of the entries our own
    `trusted_proxy_hops` proxies added is client-supplied and must be ignored.
    """
    if settings.client_ip_header:
        value = headers.get(settings.client_ip_header.lower(), "").strip()
        if value:
            return value
    hops = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if not hops:
        return None
    return hops[-min(settings.trusted_proxy_hops, len(hops))]


def _dev_keys(settings: Settings) -> dict[str, Plan]:
    out: dict[str, Plan] = {}
    for item in filter(None, (s.strip() for s in settings.dev_api_keys.split(","))):
        key, _, plan = item.partition(":")
        if plan in {"free", "pro", "credits"}:
            out[key] = plan  # type: ignore[assignment]
    return out


class InMemoryQuotaStore:
    """Dev/test only: resets on restart and is per-process."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._counts: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def _limit(self, plan: Plan) -> tuple[int | None, str]:
        if plan == "local":
            return None, "none"
        if plan == "anonymous":
            return self.settings.anon_daily_limit, "day"
        if plan == "free":
            return self.settings.free_key_monthly_limit, "month"
        if plan == "pro":
            return self.settings.pro_monthly_limit, "month"
        return None, "none"  # credits: parked with the paid tiers (BACKLOG.md "Parked")

    def consume(self, caller: Caller, now: datetime | None = None) -> Decision:
        now = now or datetime.now(UTC)
        limit, window = self._limit(caller.plan)
        if limit is None:
            return Decision(True, 0, None, window)
        bucket = now.strftime("%Y-%m-%d") if window == "day" else now.strftime("%Y-%m")
        with self._lock:
            used = self._counts[(caller.id, bucket)]
            if used >= limit:
                return Decision(False, used, limit, window)
            self._counts[(caller.id, bucket)] = used + 1
            return Decision(True, used + 1, limit, window)


def quota_message(caller: Caller, decision: Decision, settings: Settings) -> str:
    return (
        f"This hosted DepTime instance allows {decision.limit} calls per {decision.window} for {caller.plan} use "
        f"({decision.used} used). DepTime is free and open source: run it locally with no limits via "
        "`uvx deptime mcp` (see https://github.com/cschmertz/deptime). The `coverage` tool stays available."
    )


class MeteringMiddleware:
    """MCP ServerMiddleware: meters `tools/call` (except free tools) before the handler runs."""

    def __init__(self, quota: QuotaStore, settings: Settings):
        self.quota = quota
        self.settings = settings

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params or {}
        tool = params.get("name") if isinstance(params, dict) else getattr(params, "name", None)
        if tool in FREE_TOOLS:
            return await call_next(ctx)
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        client = getattr(request, "client", None)
        caller = identify(headers, getattr(client, "host", None), self.settings)
        decision = self.quota.consume(caller)
        if not decision.allowed:
            # A typed result, not a dict: spec 2026-07-28 requires `resultType`, which the model fills in.
            return CallToolResult(
                content=[TextContent(type="text", text=quota_message(caller, decision, self.settings))],
                is_error=True,
            )
        return await call_next(ctx)
