from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from deptime.config import Coverage, PackageSpec, Settings, VersionPolicy
from deptime.index.store import IndexStore
from deptime.ingest.snapshot import ApiSnapshot, SymbolRecord
from deptime.query import DepTime

PROXIES_MSG = "The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead."
HTTPX = PackageSpec(name="httpx", import_names=("httpx",), tier="B")


def _p(name: str, kind: str = "keyword-only", default: str | None = "None") -> dict:
    return {"name": name, "kind": kind, "default": default, "annotation": None}


def _snap(version: str, *, proxies: bool, old_thing: bool, new_thing: bool) -> ApiSnapshot:
    init_params = [_p("auth"), _p("proxy"), _p("mounts"), _p("timeout", default="5.0")]
    if proxies:
        init_params.insert(2, _p("proxies"))
    symbols = [
        SymbolRecord("httpx", "module", "httpx"),
        SymbolRecord("httpx.Client", "class", "httpx._client.Client"),
        SymbolRecord(
            "httpx.Client.__init__",
            "function",
            "httpx._client.Client.__init__",
            init_params,
            PROXIES_MSG if proxies else None,
        ),
        SymbolRecord(
            "httpx.Client.get",
            "function",
            "httpx._client.Client.get",
            [_p("url", "positional or keyword", None)],
        ),
        SymbolRecord(
            "httpx.request",
            "function",
            "httpx._api.request",
            [_p("method", "positional or keyword", None), _p("url", "positional or keyword", None)]
            + ([_p("proxies")] if proxies else []),
        ),
    ]
    if old_thing:
        symbols.append(
            SymbolRecord(
                "httpx.OldThing",
                "class",
                "httpx._old.OldThing",
                None,
                "OldThing is deprecated, use httpx.NewThing instead.",
            )
        )
    if new_thing:
        symbols.append(SymbolRecord("httpx.NewThing", "class", "httpx._new.NewThing"))
    return ApiSnapshot(
        package="httpx", version=version, import_names=["httpx"], symbols={s.path: s for s in symbols}
    )


SNAPSHOTS = [
    _snap("0.26.0", proxies=True, old_thing=True, new_thing=False),
    _snap("0.27.2", proxies=True, old_thing=False, new_thing=True),
    _snap("0.28.1", proxies=False, old_thing=False, new_thing=True),
]
RELEASED_AT = {
    "0.26.0": "2023-12-20T00:00:00+00:00",
    "0.27.2": "2024-08-27T00:00:00+00:00",
    "0.28.1": "2024-12-06T00:00:00+00:00",
}


@pytest.fixture
def coverage() -> Coverage:
    return Coverage(
        policy=VersionPolicy(
            since=date(2023, 1, 1), recent_months=12, include_prereleases=False, include_yanked=False
        ),
        packages=(HTTPX, PackageSpec(name="google-genai", import_names=("google.genai",), tier="A")),
    )


@pytest.fixture
def store() -> IndexStore:
    s = IndexStore.memory()
    s.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    return s


@pytest.fixture
def dt(store: IndexStore, coverage: Coverage) -> DepTime:
    return DepTime(store, coverage)


@pytest.fixture
def index_file(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    s = IndexStore.open(path, readonly=False)
    s.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    s.close()
    return path


@pytest.fixture
def settings(index_file: Path, tmp_path: Path) -> Settings:
    cov = tmp_path / "coverage.toml"
    cov.write_text(
        '[policy]\nsince = "2023-01-01"\n\n[[package]]\nname = "httpx"\nimport_names = ["httpx"]\ntier = "B"\n'
    )
    return Settings(
        index_path=index_file,
        cache_dir=tmp_path / "cache",
        coverage_file=cov,
        index_url="https://index.example.invalid",
        index_max_age_hours=24,
        offline=True,  # tests never touch the network unless they inject a MockTransport
        host="127.0.0.1",
        port=8000,
        public_url="http://test",
        trust_proxy_headers=False,
        anon_daily_limit=2,
        free_key_monthly_limit=10,
        pro_monthly_limit=100,
        dev_api_keys="pro-key:pro",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
