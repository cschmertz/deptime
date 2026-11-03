"""Mine "what to use instead" hints from deprecation text.

Deliberately conservative: a wrong replacement sends an agent down a dead end, so every hint carries the
evidence it came from and a confidence the caller can threshold. Patterns are pinned by tests.
"""

from __future__ import annotations

import builtins
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

IDENT = r"`?'?\"?(?P<t>[A-Za-z_][\w.]*(?:\(\))?)`?'?\"?"
PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(rf"\buse\s+{IDENT}\s+instead", re.I), 0.85),
    (re.compile(rf"\b(?:moved|renamed)\s+to\s+{IDENT}", re.I), 0.85),
    (re.compile(rf"\b(?:replaced|superseded)\s+(?:by|with)\s+{IDENT}", re.I), 0.8),
    (re.compile(rf"\bin\s+favou?r\s+of\s+(?:using\s+)?{IDENT}", re.I), 0.8),
    (re.compile(rf"\bnow\s+available\s+as\s+{IDENT}", re.I), 0.8),  # sqlalchemy's `became_legacy_20`
    (re.compile(rf"\bplease\s+use\s+{IDENT}", re.I), 0.75),
    (re.compile(rf"\buse\s+{IDENT}", re.I), 0.6),
]
# "`BaseSettings` has been moved to the `pydantic-settings` package": the target is a distribution (DT-CORE-06).
PACKAGE_MOVE_RE = re.compile(
    r"\bmoved\s+to\s+the\s+[`'\"]?(?P<dist>[A-Za-z0-9][\w.-]*)[`'\"]?\s+(?:package|distribution|library)\b",
    re.I,
)
MAX_EVIDENCE_CHARS = 300
LANGCHAIN_ALTERNATIVE_RE = re.compile(r"alternative(?:_import)?\s*=\s*['\"](?P<t>[\w.]+)['\"]")
QUOTED_IDENT_RE = re.compile(r"['`\"](?P<t>[A-Za-z_]\w*)['`\"]")
# Not names: "use one of …", "use `from x import y`" (DT-DATA-42 made such text readable).
STOPWORDS = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "it",
    "instead",
    "of",
    "or",
    "and",
    "one",
    "from",
    "import",
    "using",
}
# Cues that a quoted parameter is the deprecated one: "instead of 'x'", "deprecated argument 'x'",
# "'x' (argument) is (now) deprecated / will be removed / is no longer ...".
_PARAM_NOUN = r"(?:keyword\s+argument|argument|parameter|keyword|kwarg|option)"
DEPRECATED_BEFORE_RE = re.compile(rf"(?:\binstead\s+of|\bdeprecated\s+{_PARAM_NOUN})\s*$", re.I)
DEPRECATED_AFTER_RE = re.compile(
    rf"^\s*(?:{_PARAM_NOUN}\s+)?(?:is|are|was|has\s+been|will\s+be)\s+(?:now\s+)?"
    r"(?:deprecated|no\s+longer|removed|renamed|ignored)",
    re.I,
)
# A parameter named after a cue, quoted or not, behind a leading dot or before an `=` (DT-DATA-42): "please use
# log_output", "in favor of `.condition`", "Use `verify=<ssl_context>` instead".
PARAM_CUE_RE = re.compile(
    r"\b(?:use|using|pass|favou?r\s+of|replaced\s+(?:by|with)|renamed(?:\s+to)?)\s+[`'\"]?\.?(?P<t>[A-Za-z_]\w*)",
    re.I,
)
# Wording about leaving an argument out: "without state_schema", "Not providing a length argument", "without
# specifying the encoding argument".
WITHOUT_RE = re.compile(
    r"\b(?:without(?:\s+(?:passing|providing|specifying|setting))?|omitting|leaving"
    r"|not\s+(?:passing|providing|specifying|setting))\s+(?:an?\s+|the\s+)?[`'\"]?$",
    re.I,
)
# A replacement cue earlier in the same sentence: "… in favor of using … ExclusionConstraint.expressions". Not
# "Using 'method_whitelist' with Retry is deprecated" or "Use of `x` is deprecated".
CUE_BEFORE_RE = re.compile(
    r"\b(?:use(?!\s+of\b)|pass|instead|favou?r\s+of|replaced\s+(?:by|with)|renamed(?:\s+to)?)\b(?:[^.!?;]|\.(?!\s))*$",
    re.I,
)
# "Please configure the 'retry' object instead": "instead" later in the same sentence (DT-CORE-12).
INSTEAD_AFTER_RE = re.compile(r"(?:[^.!?;]|\.(?!\s))*?\binstead\b", re.I)
DEPRECATION_WORDS_RE = re.compile(r"deprecat|will be removed|no longer supported|use .+ instead", re.I)
# How libraries' own text spells an import name: numpy's "Use `np.nan` instead" (DT-CORE-06, DT-CORE-12).
ALIASES = {
    "np": "numpy",
    "pd": "pandas",
    "pl": "polars",
    "plt": "matplotlib.pyplot",
    "mpl": "matplotlib",
    "st": "streamlit",
    "gr": "gradio",
}
# A target the index contradicts stays listed with its evidence, below anything stated as advice (DT-CORE-12).
CONTRADICTED_CONFIDENCE = 0.5
# Bare names a package needn't define: "use `float`", "use `warnings`".
UNCHECKED_BARE = frozenset(dir(builtins)) | frozenset(sys.stdlib_module_names)


@dataclass(frozen=True)
class Hint:
    target: str
    confidence: float
    evidence: str


def _clean(target: str) -> str:
    return target.removesuffix("()").strip(".`'\" ")


def _private(target: str) -> bool:
    """`numpy._core.numeric`, `numpy.linalg._linalg`: DepTime indexes public API only, and a deprecation that names
    where the code went privately (numpy 2's shims) still means "use the public name"."""
    return any(part.startswith("_") and not part.endswith("__") for part in target.split("."))


def clean_evidence(text: str) -> str:
    """Untrusted text as evidence: control and format characters (bidi overrides, NULs) dropped, whitespace
    collapsed, length capped."""
    kept = "".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in text)
    return " ".join(kept.split())[:MAX_EVIDENCE_CHARS]


def moved_to_package(message: str | None) -> str | None:
    """The distribution a message says a name moved to, if it says so in these words."""
    m = PACKAGE_MOVE_RE.search(message or "")
    return m.group("dist") if m else None


def mine(message: str | None, *, skip_expressions: bool = False) -> list[Hint]:
    """Replacement candidates for a whole symbol, best first, deduplicated.

    `skip_expressions` drops a target that is only the head of a longer expression: `np.dtype` in "Use
    `np.dtype(obj).type` instead", `arr` in "Use `arr[index] = value` instead".
    """
    if not message:
        return []
    hints: dict[str, Hint] = {}
    evidence = message.strip()[:300]
    for m in LANGCHAIN_ALTERNATIVE_RE.finditer(message):
        if not _private(t := _clean(m.group("t"))):
            hints.setdefault(t, Hint(t, 0.9, evidence))
    for pattern, conf in PATTERNS:
        for m in pattern.finditer(message):
            after = message[m.end("t") : m.end("t") + 2]
            if after[:1] == "…" or (skip_expressions and after[:1] in ("(", "[", ".")):
                continue  # "Use pandas.api.typing.… instead": the snapshot couldn't read the rest of the name
            if after[:1] == "-" and after[1:].isalnum():
                continue  # "use langchain-nvidia-ai-endpoints", "use built-in …": a distribution or a word
            t = _clean(m.group("t"))
            if t and t.lower() not in STOPWORDS and t not in hints and not _private(t):
                hints[t] = Hint(t, conf, evidence)
    return sorted(hints.values(), key=lambda h: -h.confidence)


def _deprecated_at(message: str, start: int, end: int) -> bool:
    return bool(DEPRECATED_BEFORE_RE.search(message[:start]) or DEPRECATED_AFTER_RE.match(message[end:]))


def worded_deprecated(message: str | None) -> list[str]:
    """Quoted names the wording itself deprecates: "'x' is deprecated", "instead of 'x'", in order."""
    return list(
        dict.fromkeys(
            m.group("t")
            for m in QUOTED_IDENT_RE.finditer(message or "")
            if _deprecated_at(message or "", m.start(), m.end())
        )
    )


_BARE_DEPRECATED_RE = re.compile(
    rf"^\s*{_PARAM_NOUN}\s+(?:is|are|was|has\s+been|will\s+be)\s+(?:now\s+)?deprecated", re.I
)
_SENTENCE_END_RE = re.compile(r"[.!?;:](?:\s|$)")


def deprecates_name(message: str, name: str, before: Callable[[str], bool] | None = None) -> bool:
    """Whether the wording deprecates `name`: quoted or emphasized as in `worded_deprecated` ("`x` is deprecated",
    "instead of *x*"), or bare before a parameter noun ("Setting the Enum.inherit_schema parameter is
    deprecated"). A bare name alone isn't enough: "instead of a list" doesn't deprecate a parameter `a`. With
    `before`, a mention counts only if it accepts the start of the mention's sentence."""
    for m in re.finditer(rf"(?P<q>[`'\"*]+)?\b{re.escape(name)}\b(?(q)[`'\"*]+)", message):
        worded = (m.group("q") and _deprecated_at(message, m.start(), m.end())) or _BARE_DEPRECATED_RE.match(
            message[m.end() :]
        )
        if worded and (before is None or before(message[_sentence_start(message, m.start()) : m.start()])):
            return True
    return False


def _sentence_start(message: str, at: int) -> int:
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(message, 0, at)]
    return ends[-1] if ends else 0


def names_argument(message: str, name: str) -> bool:
    """Whether a deprecation notice mentions `name`, quoted or bare (`CheckConstraint.check`), other than as
    something left out ("without state_schema") or after a cue for what to use instead in the same sentence
    ("use `y_proba`")."""
    if not DEPRECATION_WORDS_RE.search(message):
        return False
    mentions = list(re.finditer(rf"\b{re.escape(name)}\b", message))
    if "_" in name:  # "Chunk size is deprecated" for `chunk_size`
        mentions += re.finditer(rf"\b{re.escape(name.replace('_', ' '))}\b", message, re.I)
    return any(
        not WITHOUT_RE.search(message[: m.start()]) and not CUE_BEFORE_RE.search(message[: m.start()])
        for m in mentions
    )


def param_alternatives(message: str, deprecated: list[str], params: list[str]) -> list[Hint]:
    """What to pass instead of the `deprecated` arguments: every other quoted name, and parameters named after a
    cue ("please use log_output", "in favor of `.condition`"), in the message's order. A parameter of the
    function scores 0.8 when its sentence has a cue before it ("Use 'proxy' or 'mounts' instead") or "instead"
    after it ("Please configure the 'retry' object instead"), anything else 0.6: polars' "…only be included if `k`
    is larger…" mentions `k` in passing (DT-CORE-12). Ties keep the message's order."""
    found = [(m.start(), m.end(), m.group("t")) for m in QUOTED_IDENT_RE.finditer(message)]
    found += [
        (m.start("t"), m.end("t"), m.group("t"))
        for m in PARAM_CUE_RE.finditer(message)
        if m.group("t") in params
    ]
    cued = {
        n
        for at, end, n in found
        if n in params and (CUE_BEFORE_RE.search(message[:at]) or INSTEAD_AFTER_RE.match(message, end))
    }
    names = [n for n in dict.fromkeys(n for _, _, n in sorted(found)) if n not in deprecated]
    evidence = message.strip()[:300]
    return [Hint(n, round((0.8 if n in cued else 0.6) - 0.01 * i, 2), evidence) for i, n in enumerate(names)]


def param_deprecations(message: str | None, params: list[str]) -> dict[str, list[Hint]]:
    """Parameter-level reading of a function's deprecation text.

    "The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead." on a function whose
    parameters include `proxies`, `proxy`, `mounts` -> {"proxies": [Hint("proxy"), Hint("mounts")]}.

    A parameter counts as deprecated only when the wording says so ("'x' is deprecated", "instead of 'x'"),
    never by position: "Use 'proxy' instead of 'proxies'" deprecates `proxies`, and a whole-function notice
    that merely quotes a parameter ("use `model_copy` ... handle `include`") stays a function deprecation.
    """
    if not message:
        return {}
    deprecated = [n for n in worded_deprecated(message) if n in params]
    if not deprecated:
        return {}
    hints = param_alternatives(message, deprecated, params)
    return {p: list(hints) for p in deprecated}


def expand_alias(target: str) -> str:
    """`np.result_type` -> `numpy.result_type`."""
    head, _, rest = target.partition(".")
    return f"{ALIASES[head]}.{rest}" if rest and head in ALIASES else target


def _ancestors(path: str) -> list[str]:
    parts = path.split(".")
    return [".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


class PublicNames:
    """One package's public paths as its index rows have them, to check what a hint names (DT-CORE-12).

    `spans` are (path, last indexed ordinal, object): the object is the path's canonical definition, or the path
    itself for a name a `__getattr__` serves (they all share its canonical).
    """

    def __init__(self, import_names: Sequence[str], spans: Iterable[tuple[str, int, str | None]]):
        self.import_names = tuple(import_names)
        self._last: dict[str, int] = {}
        self._object: dict[str, str] = {}
        for path, last, obj in spans:
            if last >= self._last.get(path, -1):
                self._last[path], self._object[path] = last, obj or path
        self._tails: dict[str, list[str]] | None = None

    def _own(self, target: str) -> bool:
        return any(target == n or target.startswith(n + ".") for n in self.import_names)

    def _found(self, candidate: str, path: str, since: int) -> bool:
        """`candidate` is public at release `since` or later. It can be the deprecated object at another path:
        numpy 2's `numpy.linalg.linalg.det` says to import `numpy.linalg.det`."""
        return self._last.get(candidate, -1) >= since and candidate != path

    def _by_tail(self, name: str, path: str, since: int) -> list[str]:
        """Paths ending in `name` public at `since` or later, other than the deprecated object itself: a bare
        `product` doesn't mean `numpy.core.fromnumeric.product` on `numpy.product`."""
        if self._tails is None:
            self._tails = defaultdict(list)
            for p in self._last:
                self._tails[p.rpartition(".")[2]].append(p)
        same = self._object.get(path, path)
        return [
            p for p in self._tails.get(name, ()) if self._found(p, path, since) and self._object[p] != same
        ]

    def _one(self, paths: list[str]) -> str | None:
        """The shortest of `paths` if they're all one object (re-exports of it), else None."""
        if len({self._object[p] for p in paths}) != 1:
            return None
        return min(paths, key=lambda p: (p.count("."), len(p), p))

    def resolve(self, target: str, path: str, since: int) -> tuple[str, bool | None]:
        """(target, verdict) for a hint on `path` from a deprecation first seen at ordinal `since`.

        True: the index has it; the target comes back as the public path it means (`prod` on `numpy.product` ->
        `numpy.prod`), or as written if several objects have that name. False: the index contradicts it, a path in
        the package's own namespace it doesn't have, or a bare name no indexed release has. None: it's outside
        the package (another distribution, the standard library, builtins, `name.map` on an instance), or a bare
        name only earlier releases have, so the index can't say.

        Tried in order: as written, if it's in the package's namespace (else, for a dotted name, the same leaf
        next to `path`); under each of `path`'s ancestors, nearest first, and the package's top level; then a
        bare name anywhere in the package, or a dotted one under any object named like its head.
        """
        target = expand_alias(target)
        if self._own(target):
            if self._found(target, path, since):
                return target, True
            sibling = f"{path.rpartition('.')[0]}.{target.rpartition('.')[2]}"
            return (sibling, True) if "." in target and self._found(sibling, path, since) else (target, False)
        for scope in dict.fromkeys([*_ancestors(path), *self.import_names]):
            if self._found(candidate := f"{scope}.{target}", path, since):
                return candidate, True
        head, _, rest = target.partition(".")
        if not rest:
            if found := self._by_tail(target, path, since):
                return self._one(found) or target, True
            if target in UNCHECKED_BARE or self._by_tail(target, path, 0):
                return target, None  # or one the index lost track of: an inherited member from elsewhere
            return target, False
        found = [c for h in self._by_tail(head, path, since) if self._found(c := f"{h}.{rest}", path, since)]
        return (self._one(found) or target, True) if found else (target, None)

    def check(self, hint: Hint, path: str, since: int) -> Hint:
        """`hint` with its target qualified, and its confidence capped if the index contradicts it."""
        target, verdict = self.resolve(hint.target, path, since)
        confidence = (
            hint.confidence if verdict is not False else min(hint.confidence, CONTRADICTED_CONFIDENCE)
        )
        return Hint(target, confidence, hint.evidence)
