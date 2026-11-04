"""Migration hints from the projects' own documentation (DT-DATA-08): the packaged `deptime/curated.toml`.

Each pairs a name the index has with what its project's own migration guide or changelog says to use instead,
quoted, with the page's URL. Some names never carried a deprecation the snapshots can read (mistralai 1.0 replaced
`MistralClient` without one), and some replacements live in another distribution (`google.generativeai` ->
`google.genai`, DT-DATA-07). They enter the index at assembly with `source="curated"`. Per ADR 0006 each ships
only after mechanical checks: the quote is on the page, names both names, and the index has both (the header of
`curated.toml` lists them).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources

from packaging.utils import canonicalize_name

CURATED_CONFIDENCE = 0.9  # the project's own documentation, checked against the index


@dataclass(frozen=True)
class CuratedHint:
    package: str  # the covered distribution that has `path`
    path: str  # an indexed public path, or `path(param)` for an argument
    target: str  # a public path, possibly another covered package's, or for an argument the parameter to pass
    quote: str  # what the source says, verbatim
    url: str

    @property
    def evidence(self) -> str:
        return f"{self.quote} ({self.url})"


@cache
def curated_hints() -> tuple[CuratedHint, ...]:
    raw = tomllib.loads(resources.files("deptime").joinpath("curated.toml").read_text(encoding="utf-8"))
    return tuple(
        CuratedHint(h["package"], h["path"], h["target"], h["quote"], h["url"]) for h in raw.get("hint", [])
    )


def hints_for(package: str) -> tuple[CuratedHint, ...]:
    name = canonicalize_name(package)
    return tuple(h for h in curated_hints() if canonicalize_name(h.package) == name)
