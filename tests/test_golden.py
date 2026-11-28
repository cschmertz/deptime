"""Golden tests against the published index (DT-QA-03). Opt-in: `deptime index pull`, then
`uv run pytest -m published`. bench.yml runs them after every build, and on code changes.

The benchmark checks old boundaries; these check the newest releases: every covered package is indexed, its newest
release on PyPI is indexed within the freshness bound (DT-DATA-04), and each Tier A package's newest indexed release
has the names its own documentation uses (`golden/tier_a.toml`). `golden/working.toml` has names found working by
running a release, which must not read `missing` there (DT-DATA-103).
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from packaging.version import Version

from deptime.config import Settings, load_coverage
from deptime.index.location import active_index, open_index
from deptime.ingest import pypi
from deptime.ingest.pipeline import FRESHNESS_HOURS
from deptime.models import SymbolQuery
from deptime.query import DepTime

GOLDEN = tomllib.loads((Path(__file__).parent / "golden" / "tier_a.toml").read_text(encoding="utf-8"))[
    "package"
]
WORKING = tomllib.loads((Path(__file__).parent / "golden" / "working.toml").read_text(encoding="utf-8"))[
    "case"
]
COVERAGE = load_coverage()
# Twice-daily builds start hours late (DT-DATA-04); older than this, several runs in a row failed to publish.
MAX_INDEX_AGE = timedelta(hours=48)


@pytest.fixture(scope="module")
def published() -> DepTime:
    settings = Settings.from_env()
    store = open_index(settings.index_path)
    if store is None:
        pytest.fail(f"no index at {active_index(settings.index_path)}: run `deptime index pull` first")
    return DepTime(store, COVERAGE, settings.index_path)


@pytest.fixture(scope="module")
def built_at(published: DepTime) -> datetime:
    return datetime.fromisoformat(published.store.built_at())


@pytest.fixture(scope="module")
def pypi_client():
    with pypi.http_client() as client:
        yield client


def test_the_golden_file_covers_every_tier_a_package():
    tier_a = sorted(p.name for p in COVERAGE.packages if p.tier == "A")
    assert sorted(g["name"] for g in GOLDEN) == tier_a
    assert all(g["present"] and g["evidence"].startswith("https://") for g in GOLDEN)


@pytest.mark.published
def test_the_published_index_is_recent_and_covers_every_package(published: DepTime, built_at: datetime):
    assert datetime.now(UTC) - built_at < MAX_INDEX_AGE, f"the published index was built {built_at}"
    report = published.coverage_report()
    assert report.configured_but_not_indexed == []
    assert all(entry.versions_indexed > 0 for entry in report.packages)


@pytest.mark.published
@pytest.mark.parametrize("golden", GOLDEN, ids=[g["name"] for g in GOLDEN])
def test_the_newest_release_has_the_documented_names(published: DepTime, golden: dict):
    entry = next(e for e in published.coverage_report().packages if e.package == golden["name"])
    result = published.check_symbols(
        golden["name"], entry.latest_version, [SymbolQuery(path=p) for p in golden["present"]]
    )
    missing = [r.path for r in result.results if r.status != "present"]
    assert not missing, f"{golden['name']} {entry.latest_version} lacks {missing} ({golden['evidence']})"


def test_each_working_case_says_how_it_was_checked():
    covered = {p.name for p in COVERAGE.packages}
    for case in WORKING:
        assert case["package"] in covered and case["evidence"]
        assert case.get("present") or case.get("not_missing")
        assert set(case) <= {"package", "version", "present", "not_missing", "missing", "evidence"}


@pytest.mark.published
@pytest.mark.parametrize("case", WORKING, ids=[f"{c['package']}-{c['version']}" for c in WORKING])
def test_names_that_work_at_a_release_dont_read_missing(published: DepTime, case: dict):
    """A name that runs and reads `missing` fails a clean update (DT-DATA-103); one of `missing` that doesn't shows
    the release answered `unknown` for more than it had to."""
    wanted = {
        "present": case.get("present", []),
        "not_missing": case.get("not_missing", []),
        "missing": case.get("missing", []),
    }
    paths = [p for group in wanted.values() for p in group]
    result = published.check_symbols(case["package"], case["version"], [SymbolQuery(path=p) for p in paths])
    assert result.exact, f"{case['package']} {case['version']} isn't indexed"
    status = {r.path: r.status for r in result.results}
    wrong = (
        [f"{p}: {status[p]}, not present" for p in wanted["present"] if status[p] != "present"]
        + [f"{p}: missing" for p in wanted["not_missing"] if status[p] == "missing"]
        + [f"{p}: {status[p]}, not missing" for p in wanted["missing"] if status[p] != "missing"]
    )
    assert not wrong, f"{case['package']} {case['version']}: {wrong} ({case['evidence']})"


@pytest.mark.published
@pytest.mark.parametrize("name", [p.name for p in COVERAGE.packages])
def test_the_newest_release_is_indexed_within_the_freshness_bound(
    published: DepTime, built_at: datetime, pypi_client: httpx.Client, name: str
):
    """Every release the policy selects that PyPI had more than FRESHNESS_HOURS before the build is indexed,
    up to the newest: an older one that can't be analysed is the coverage summary's business (DT-DATA-01)."""
    indexed = [r["version"] for r in published.store.releases(name)]
    newest = max(Version(v) for v in indexed)
    selected = pypi.select_versions(pypi.fetch_releases(name, pypi_client), COVERAGE.policy, built_at.date())
    due = built_at - timedelta(hours=FRESHNESS_HOURS)
    late = [
        str(r.version)
        for r in selected
        if r.version > newest and r.uploaded_at is not None and r.uploaded_at < due
    ]
    assert not late, (
        f"{name}: uploaded more than {FRESHNESS_HOURS} h before the build ({built_at}) but not indexed: {late}"
    )
