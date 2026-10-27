"""End-to-end against real PyPI wheels. Opt-in: `uv run pytest -m network`."""

from datetime import date

import pytest

from deptime.config import Coverage, PackageSpec, VersionPolicy
from deptime.index.store import IndexStore
from deptime.ingest.pipeline import ingest_package
from deptime.query import DepTime
from tests.test_tracebacks import HTTPX_TB

pytestmark = pytest.mark.network


def test_httpx_proxies_removal_end_to_end(tmp_path):
    spec = PackageSpec(name="httpx", import_names=("httpx",), tier="B")
    policy = VersionPolicy(date(2023, 1, 1), 12, False, False)
    store = IndexStore.memory()
    report = ingest_package(spec, policy, store, tmp_path, versions=["0.27.2", "0.28.1"], log=lambda _m: None)
    assert report.failed == {} and report.stats.releases == 2

    res = DepTime(store, Coverage(policy, (spec,))).explain_error(HTTPX_TB, {"httpx": "0.28.1"})
    top = res.findings[0]
    assert (top.symbol, top.issue, top.last_version_with) == (
        "httpx.Client.__init__(proxies)",
        "param_removed",
        "0.27.2",
    )
    assert {"proxy", "mounts"} <= {r.target for r in top.replacements}
