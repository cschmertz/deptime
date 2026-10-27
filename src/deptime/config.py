"""Runtime settings (from environment) and the coverage list (packaged `deptime/coverage.toml`)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

from packaging.utils import canonicalize_name

# Rolling GitHub Release that CI refreshes nightly (.github/workflows/build-index.yml).
DEFAULT_INDEX_URL = "https://github.com/cschmertz/deptime/releases/download/index"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def default_home() -> Path:
    """Where DepTime keeps its index and build cache: $DEPTIME_HOME, else $XDG_CACHE_HOME/deptime, else ~/.cache/deptime."""
    explicit = os.environ.get("DEPTIME_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "deptime"


@dataclass(frozen=True)
class Settings:
    index_path: Path  # the SQLite index the tools read
    cache_dir: Path  # ingestion build cache: artifacts, extracted wheels, snapshots
    coverage_file: Path | None  # None = the coverage list packaged with deptime
    index_url: str  # base URL serving manifest.json + index.sqlite.gz
    index_max_age_hours: int  # how stale a *downloaded* index may get before a refresh check
    offline: bool  # never touch the network (no index download/refresh)
    # --- hosted mode only (local stdio mode never meters) ---
    host: str
    port: int
    public_url: str
    trust_proxy_headers: bool
    anon_daily_limit: int
    free_key_monthly_limit: int
    pro_monthly_limit: int
    dev_api_keys: str  # "key:plan,key2:plan"; a real key store is parked with the paid tiers
    # With trust_proxy_headers: proxies *append* to X-Forwarded-For, so the client is this many hops from the right.
    trusted_proxy_hops: int = 1
    client_ip_header: str = ""  # e.g. "Fly-Client-IP": a header your platform's proxy sets (wins over XFF)

    @classmethod
    def from_env(cls) -> Settings:
        home = default_home()
        coverage = os.environ.get("DEPTIME_COVERAGE_FILE", "").strip()
        return cls(
            index_path=Path(_env("DEPTIME_INDEX_PATH", str(home / "index.sqlite"))).expanduser(),
            cache_dir=Path(_env("DEPTIME_CACHE_DIR", str(home / "build"))).expanduser(),
            coverage_file=Path(coverage).expanduser() if coverage else None,
            index_url=_env("DEPTIME_INDEX_URL", DEFAULT_INDEX_URL).rstrip("/"),
            index_max_age_hours=_env_int("DEPTIME_INDEX_MAX_AGE_HOURS", 24),
            offline=_env_bool("DEPTIME_OFFLINE", False),
            host=_env("DEPTIME_HOST", "127.0.0.1"),
            port=_env_int("DEPTIME_PORT", 8000),
            public_url=_env("DEPTIME_PUBLIC_URL", "http://127.0.0.1:8000"),
            trust_proxy_headers=_env_bool("DEPTIME_TRUST_PROXY_HEADERS", False),
            anon_daily_limit=_env_int("DEPTIME_ANON_DAILY_LIMIT", 25),
            free_key_monthly_limit=_env_int("DEPTIME_FREE_KEY_MONTHLY_LIMIT", 1500),
            pro_monthly_limit=_env_int("DEPTIME_PRO_MONTHLY_LIMIT", 20000),
            dev_api_keys=_env("DEPTIME_DEV_API_KEYS", ""),
            trusted_proxy_hops=max(1, _env_int("DEPTIME_TRUSTED_PROXY_HOPS", 1)),
            client_ip_header=_env("DEPTIME_CLIENT_IP_HEADER", ""),
        )


@dataclass(frozen=True)
class VersionPolicy:
    since: date
    recent_months: int
    include_prereleases: bool
    include_yanked: bool

    def describe(self) -> str:
        return (
            f"first+last release of every minor series active since {self.since.isoformat()}, "
            f"plus every release from the last {self.recent_months} months; "
            f"pre-releases {'included' if self.include_prereleases else 'excluded'}, "
            f"yanked {'included' if self.include_yanked else 'excluded'}"
        )


@dataclass(frozen=True)
class PackageSpec:
    name: str  # canonical PEP 503 name
    import_names: tuple[str, ...]
    tier: str
    notes: str = ""


@dataclass(frozen=True)
class Coverage:
    policy: VersionPolicy
    packages: tuple[PackageSpec, ...]

    def get(self, name: str) -> PackageSpec | None:
        wanted = canonicalize_name(name)
        return next((p for p in self.packages if p.name == wanted), None)

    def packages_for_module(self, module: str) -> list[PackageSpec]:
        """Packages whose import names are the longest dotted prefix of `module`.

        `google.genai.types` -> google-genai; `agents.run` -> openai-agents. Several packages can tie
        (import-name collisions); callers disambiguate with installed_versions.
        """
        best_len, best = 0, []
        for spec in self.packages:
            for import_name in spec.import_names:
                if module == import_name or module.startswith(import_name + "."):
                    n = import_name.count(".") + 1
                    if n > best_len:
                        best_len, best = n, [spec]
                    elif n == best_len and spec not in best:
                        best.append(spec)
        return best


def load_coverage(path: Path | None = None) -> Coverage:
    """Load a coverage file, or the list packaged with deptime when `path` is None."""
    text = (
        path.read_text(encoding="utf-8")
        if path is not None
        else resources.files("deptime").joinpath("coverage.toml").read_text(encoding="utf-8")
    )
    raw = tomllib.loads(text)
    pol = raw.get("policy", {})
    policy = VersionPolicy(
        since=date.fromisoformat(pol.get("since", "2023-01-01")),
        recent_months=int(pol.get("recent_months", 12)),
        include_prereleases=bool(pol.get("include_prereleases", False)),
        include_yanked=bool(pol.get("include_yanked", False)),
    )
    packages = tuple(
        PackageSpec(
            name=canonicalize_name(p["name"]),
            import_names=tuple(p.get("import_names") or [canonicalize_name(p["name"]).replace("-", "_")]),
            tier=p.get("tier", "B"),
            notes=p.get("notes", ""),
        )
        for p in raw.get("package", [])
    )
    return Coverage(policy=policy, packages=packages)
