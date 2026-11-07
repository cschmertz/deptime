"""Rename-map pages generated from the index (DT-GTM-01)."""

from deptime.models import Replacement
from deptime.site import Migration, Range, _evidence, build


def test_a_rename_map_lists_what_replaces_each_name(dt, tmp_path):
    migration = Migration("httpx-0.26-to-0.28", "httpx 0.26 → 0.28", (Range("httpx", "0.26."),))
    written = build(dt, tmp_path, (migration,), base_url="https://example.invalid/")
    assert {p.name for p in written} == {"httpx-0.26-to-0.28.html", "index.html", "llms.txt", "sitemap.xml"}
    page = (tmp_path / "httpx-0.26-to-0.28.html").read_text(encoding="utf-8")
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
