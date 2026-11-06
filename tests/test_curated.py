"""The migration hints shipped in `deptime/curated.toml` (DT-DATA-08) and the checks each must pass (ADR 0006)."""

import html
import os
import re
from html.parser import HTMLParser

import httpx
import pytest

from deptime import stdlib
from deptime.config import load_coverage
from deptime.ingest.curated import curated_hints


def test_every_curated_hint_is_well_formed():
    coverage = load_coverage()
    hints = curated_hints()
    assert hints, "curated.toml ships no hints"
    seen = set()
    for h in hints:
        func, _, rest = h.path.partition("(")
        if h.package == "python":  # the standard library's own removals (DT-DATA-48)
            module, _, name = func.rpartition(".")
            entry, named = stdlib.lookup(func), stdlib.lookup_name(module, name)
            assert (entry is not None and entry.module == func and entry.last is not None) or (
                named is not None and named.ranges[-1][1] is not None
            ), f"{func} isn't a removed standard module or name"
        else:
            spec = coverage.get(h.package)
            assert spec is not None, f"{h.package} is not covered"
            assert any(func == n or func.startswith(n + ".") for n in spec.import_names), h.path
        if rest:  # an argument: `path(param)`, and the target is the parameter to pass instead
            assert rest.endswith(")") and rest[:-1].isidentifier(), h.path
            assert h.target.isidentifier(), (h.path, h.target)
        else:
            assert all(part.isidentifier() for part in h.target.split(".")), (h.path, h.target)
        assert h.url.startswith("https://"), h.url
        assert 0 < len(h.quote) <= 300 and "\n" not in h.quote, h.path
        assert (h.package, h.path, h.target) not in seen, h.path
        seen.add((h.package, h.path, h.target))


def _last_name(path: str) -> str:
    """The name a quote has to mention: an argument, a class for its `__init__`, else the last part."""
    func, _, param = path.partition("(")
    if param:
        return param.removesuffix(")")
    parts = func.split(".")
    return parts[-2] if parts[-1] == "__init__" else parts[-1]


# Names a source spells differently from the code, and how the quote spells them.
SPELLED = {
    "completion_stream": "completions_stream"
}  # mistralai's MIGRATION.md, for `MistralClient.completion_stream`


def test_every_curated_quote_names_the_old_name_and_its_replacement():
    # Case aside: selenium's guide says "a Service object" for the `service` argument.
    unnamed = []
    for h in curated_hints():
        quote = h.quote.lower()
        target = h.target if "(" in h.path else _last_name(h.target)
        for name in (_last_name(h.path), target):
            if SPELLED.get(name, name).lower() not in quote:
                unnamed.append(f"{h.path} -> {h.target}: the quote doesn't name {name}")
    assert not unnamed, "\n".join(unnamed)


class _Text(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "svg", "head"})
    BLOCK = frozenset(
        {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "br", "dt", "dd"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    for attempt in range(3):
        try:
            resp = client.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise AssertionError(f"can't fetch {url}: {exc!r}") from exc
    raise AssertionError("unreachable")


def _page(client: httpx.Client, url: str) -> tuple[str, str | None]:
    """The text at `url` and, for a GitHub file with `#Lx-Ly`, those lines (3 lines of slack either side).

    A GitHub file is read raw, a release through the API (its body is markdown), an HTML page as its text.
    A Python file's adjacent string literals are joined, so a quote can be the message Python builds from them.
    """
    base, _, frag = url.partition("#")
    lines = None
    if m := re.match(r"https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.+)", base):
        repo, ref, path = m.groups()
        resp = _get(client, f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")
        text = resp.text
        if path.endswith(".ipynb"):
            text = "\n\n".join("".join(c["source"]) for c in resp.json()["cells"])
        if lm := re.fullmatch(r"L(\d+)(?:-L(\d+))?", frag):
            first, last = int(lm.group(1)), int(lm.group(2) or lm.group(1))
            lines = "\n".join(text.splitlines()[max(0, first - 4) : last + 3])
        if path.endswith(".py"):
            text, lines = (re.sub(r"(?<=[^\\])([\"'])\s*\n?\s*\1", "", t) if t else t for t in (text, lines))
        return text, lines
    if m := re.match(r"https://github\.com/([^/]+/[^/]+)/releases/tag/(.+)", base):
        token = os.environ.get("GITHUB_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = _get(
            client, f"https://api.github.com/repos/{m.group(1)}/releases/tags/{m.group(2)}", headers=headers
        )
        return resp.json()["body"], None
    resp = _get(client, base)
    if "html" not in resp.headers.get("content-type", ""):
        return resp.text, None  # e.g. aistudio.google.com serves its docs to non-browsers as markdown
    parser = _Text()
    parser.feed(resp.text)
    return "".join(parser.parts), None


def _norm(s: str) -> str:
    """Compare text, not markup: entities, typographic quotes, markdown escapes, links, code and emphasis marks."""
    s = html.unescape(s).translate(
        str.maketrans(
            {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u00b6": " "}
        )
    )
    s = re.sub(r"\\([_*`\[\]()#.!-])", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return " ".join(s.replace("`", "").replace("*", "").split())


def _quoted(quote: str, text: str) -> bool:
    """Each excerpt of the quote (" … " or " [...] " leaves text out) appears in `text`, in order. In a table row,
    " | " separates cells, which a rendered page shows without the bars."""
    excerpts = [x for x in re.split(r"\s*(?:…|\[\.\.\.\])\s*", quote) if x.strip()]
    page = _norm(text)
    for cells in (False, True):
        pos = 0
        for excerpt in excerpts:
            want = _norm(excerpt.replace(" | ", " ").strip(" |") if cells else excerpt)
            found = page.find(want, pos)
            if found < 0:
                break
            pos = found + len(want)
        else:
            return True
    return False


@pytest.mark.network
def test_every_curated_quote_appears_verbatim_at_its_url():
    pages: dict[str, tuple[str, str | None]] = {}
    wrong = []
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for h in curated_hints():
            if h.url not in pages:
                pages[h.url] = _page(client, h.url)
            text, lines = pages[h.url]
            if not _quoted(h.quote, text):
                wrong.append(f"{h.path}: not at {h.url}")
            elif lines is not None and not _quoted(h.quote, lines):
                wrong.append(
                    f"{h.path}: at {h.url.partition('#')[0]}, but not in lines {h.url.partition('#')[2]}"
                )
    assert not wrong, "\n".join(wrong)
