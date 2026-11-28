"""Rename-map pages generated from the index (DT-GTM-01)."""

from pathlib import Path

import pytest

from deptime.config import Coverage
from deptime.index.store import IndexStore
from deptime.ingest.snapshot import SymbolRecord
from deptime.models import Replacement
from deptime.query import DepTime
from deptime.site import MIGRATIONS, Migration, Range, _code, _evidence, build
from tests.conftest import HTTPX, RELEASED_AT, _snap

SDIST = (Path(__file__).resolve().parents[1] / "PKG-INFO").is_file()


def _read(path) -> str:
    """A page's HTML without the soft line breaks code paths carry after each dot."""
    return path.read_text(encoding="utf-8").replace("<wbr>", "")


def test_a_rename_map_lists_what_replaces_each_name(dt, tmp_path):
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    written = build(dt, tmp_path, (migration,), base_url="https://example.invalid/")
    assert {p.name for p in written} == {"httpx-0.26-to-0.28.html", "index.html", "llms.txt", "sitemap.xml"}
    page = _read(tmp_path / "httpx-0.26-to-0.28.html")
    assert "httpx 0.26.0 → 0.28.1" in page
    assert "<code>httpx.OldThing</code></td><td><code>httpx.NewThing</code>" in page
    assert (
        "<code>httpx.Client.__init__(proxies)</code></td><td><code>proxy</code> or <code>mounts</code>"
        in page
    )
    assert "- [httpx 0.26 → 0.28](httpx-0.26-to-0.28.html): 2 names replaced, 0 removed" in (
        tmp_path / "llms.txt"
    ).read_text(encoding="utf-8")
    assert "https://example.invalid/httpx-0.26-to-0.28.html" in (tmp_path / "sitemap.xml").read_text(
        encoding="utf-8"
    )


def test_library_text_is_escaped_on_the_page():
    r = Replacement(target="x", confidence=0.9, source="deprecation_message", evidence="use <b>x</b> & not y")
    assert "&lt;b&gt;x&lt;/b&gt; &amp; not y" in _evidence(r)


def test_each_page_has_a_search_title_description_and_canonical_url(dt, tmp_path):
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    build(dt, tmp_path, (migration,), base_url="https://example.invalid/")
    page = _read(tmp_path / "httpx-0.26-to-0.28.html")
    assert "<title>httpx 0.26 → 0.28 migration: renamed and removed names</title>" in page
    assert '<meta name="description" content="httpx 0.26 → 0.28: 2 names and arguments' in page
    assert '<link rel="canonical" href="https://example.invalid/httpx-0.26-to-0.28.html">' in page
    assert "uvx deptime check-upgrade" in page  # the way back to DepTime from a search
    landing = _read(tmp_path / "index.html")
    assert '<link rel="canonical" href="https://example.invalid/">' in landing
    assert '<a class="card" href="httpx-0.26-to-0.28.html"><h3>httpx 0.26 → 0.28</h3>' in landing
    assert "<b>2</b> renamed or replaced</span><span><b>0</b> removed" in landing
    assert f"of {len(dt.coverage.packages)} fast-moving Python libraries" in landing


def test_without_a_base_url_there_is_no_canonical_url_or_sitemap(dt, tmp_path):
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    written = build(dt, tmp_path, (migration,))
    assert "sitemap.xml" not in {p.name for p in written}
    assert 'rel="canonical"' not in (tmp_path / "httpx-0.26-to-0.28.html").read_text(encoding="utf-8")


def test_paths_no_one_writes_are_left_off_the_maps():
    from deptime.site import _artefact

    assert _artefact("pydantic.v1.v1.BaseModel")  # a module seen through itself
    assert _artefact("pydantic.config.DeprecationWarning")  # a builtin the module imports
    assert not _artefact("pydantic.v1.BaseModel")
    assert not _artefact("httpx.Client.__init__")
    assert not _artefact("langchain_core.tools.BaseTool.list")
    assert not _artefact("requests.ConnectionError")


def _with(version: str, *names: str, **flags: bool):
    """The synthetic httpx release `version` with extra top-level functions."""
    snap = _snap(version, **flags)
    for name in names:
        snap.symbols[f"httpx.{name}"] = SymbolRecord(f"httpx.{name}", "function", f"httpx._api.{name}", [])
    return snap


def test_a_name_that_comes_back_or_that_no_one_writes_is_not_listed_as_removed(coverage: Coverage, tmp_path):
    """gradio 6.20's stubs lost every component's `click` and 6.27's had it again: not a removal of the migration.
    A `_private` name or a module's `__all__` going away is no one's broken code."""
    store = IndexStore.memory()
    store.rebuild_package(
        HTTPX,
        [
            _with(
                "0.26.0", "gone", "back", "_hidden", "__all__", proxies=True, old_thing=True, new_thing=False
            ),
            _with("0.27.2", proxies=True, old_thing=False, new_thing=True),
            _with("0.28.1", "back", proxies=False, old_thing=False, new_thing=True),
        ],
        RELEASED_AT,
    )
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    build(DepTime(store, coverage), tmp_path, (migration,))
    page = _read(tmp_path / "httpx-0.26-to-0.28.html")
    assert '<tr><td><code>httpx.gone</code></td><td class="v">0.27.2</td></tr>' in page
    assert "httpx.back" not in page
    assert "httpx._hidden" not in page and "httpx.__all__" not in page
    assert "<code>httpx.Client.__init__(proxies)</code>" in page  # a removed argument of `__init__` stays


def test_the_landing_page_describes_many_maps_in_one_short_sentence(dt, tmp_path):
    many = tuple(Migration(f"m{i}", f"Map {i}", (Range("httpx", "0.26."),)) for i in range(8))
    build(dt, tmp_path, many)
    landing = _read(tmp_path / "index.html")
    assert "Plus rename maps: Map 0, Map 1, Map 2, Map 3, Map 4 and 3 more." in landing
    assert landing.count('<a class="card"') == 8


def test_every_shipped_map_has_its_own_address_and_a_covered_package():
    from deptime.config import load_coverage

    covered = {p.name for p in load_coverage().packages}
    assert len({m.slug for m in MIGRATIONS}) == len(MIGRATIONS) == len({m.title for m in MIGRATIONS})
    for m in MIGRATIONS:
        assert m.ranges or m.curated_from, m.slug
        assert {r.package for r in m.ranges} <= covered, m.slug


@pytest.mark.skipif(SDIST, reason="docs/assets is not included in the sdist")
def test_pages_carry_the_brand_link_previews_and_a_filter(dt, tmp_path):
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    build(dt, tmp_path, (migration,), base_url="https://example.invalid")
    for name in ("deptime-icon.svg", "deptime-wordmark-light.svg", "upgrade-dark.svg", "social-preview.png"):
        assert (tmp_path / name).is_file()  # copied from docs/assets
    page = _read(tmp_path / "httpx-0.26-to-0.28.html")
    assert '<meta property="og:image" content="https://example.invalid/social-preview.png">' in page
    assert '<link rel="icon" href="deptime-icon.svg" type="image/svg+xml">' in page
    assert '<input id="filter"' in page and 'placeholder="Filter names, e.g. OldThing"' in page
    assert '<a href="./#maps">Rename maps</a>' in page  # every map leads back


def test_without_the_assets_folder_the_pages_still_build(dt, tmp_path):
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    build(dt, tmp_path, (migration,), base_url="https://example.invalid", assets=tmp_path / "missing")
    assert not (tmp_path / "social-preview.png").exists()
    assert "og:image" not in _read(tmp_path / "httpx-0.26-to-0.28.html")


def test_a_code_path_wraps_only_after_a_dot():
    assert _code("a.b.c(x)") == "<code>a.<wbr>b.<wbr>c<wbr>(x)</code>"


def test_backticked_names_in_library_text_show_as_code_and_stay_escaped():
    r = Replacement(
        target="x", confidence=0.9, source="deprecation_message", evidence="use `New` not `<script>` & `Old`"
    )
    out = _evidence(r)
    assert "<code>New</code>" in out and "<code>Old</code>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_the_footer_gives_a_plain_date_and_no_claim_about_hand_writing(dt, tmp_path):
    import re

    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    build(dt, tmp_path, (migration,))
    for name in ("index.html", "httpx-0.26-to-0.28.html"):
        page = _read(tmp_path / name)
        assert "by hand" not in page
        assert "UTC" not in page
        assert re.search(r"Last updated [A-Z][a-z]+ \d{1,2}, \d{4}</span>", page)
    assert "read from each release's own code" in _read(tmp_path / "httpx-0.26-to-0.28.html")


def test_the_page_script_declares_no_name_the_browser_already_owns():
    """A top-level `const top` (or `name`, `status`, ...) is a SyntaxError in a browser, which stops the whole
    script: copy buttons, the map filter and the header. Node accepts it, so only this check catches it."""
    import re

    from deptime.site import SCRIPT

    owned = {
        "top",
        "name",
        "status",
        "parent",
        "self",
        "length",
        "origin",
        "frames",
        "opener",
        "closed",
        "event",
    }
    declared = set(re.findall(r"(?:^|[;{(\s])(?:const|let|var)\s+(\w+)", SCRIPT))
    declared |= set(re.findall(r",\s*(\w+)\s*=", SCRIPT))
    assert not declared & owned
