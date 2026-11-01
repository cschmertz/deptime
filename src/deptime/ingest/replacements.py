"""Mine "what to use instead" hints from deprecation text.

Deliberately conservative: a wrong replacement sends an agent down a dead end, so every hint carries the
evidence it came from and a confidence the caller can threshold. Patterns are pinned by tests.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

IDENT = r"`?'?\"?(?P<t>[A-Za-z_][\w.]*(?:\(\))?)`?'?\"?"
PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(rf"\buse\s+{IDENT}\s+instead", re.I), 0.85),
    (re.compile(rf"\b(?:moved|renamed)\s+to\s+{IDENT}", re.I), 0.85),
    (re.compile(rf"\b(?:replaced|superseded)\s+(?:by|with)\s+{IDENT}", re.I), 0.8),
    (re.compile(rf"\bin\s+favou?r\s+of\s+{IDENT}", re.I), 0.8),
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
STOPWORDS = {"the", "a", "an", "this", "that", "it", "instead", "of", "or", "and"}
# Cues that a quoted parameter is the deprecated one: "instead of 'x'", "deprecated argument 'x'",
# "'x' (argument) is (now) deprecated / will be removed / is no longer ...".
_PARAM_NOUN = r"(?:keyword\s+argument|argument|parameter|keyword|kwarg|option)"
DEPRECATED_BEFORE_RE = re.compile(rf"(?:\binstead\s+of|\bdeprecated\s+{_PARAM_NOUN})\s*$", re.I)
DEPRECATED_AFTER_RE = re.compile(
    rf"^\s*(?:{_PARAM_NOUN}\s+)?(?:is|are|was|has\s+been|will\s+be)\s+(?:now\s+)?"
    r"(?:deprecated|no\s+longer|removed|renamed|ignored)",
    re.I,
)


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
            if skip_expressions and message[m.end("t") : m.end("t") + 1] in ("(", "[", "."):
                continue
            t = _clean(m.group("t"))
            if t and t.lower() not in STOPWORDS and t not in hints and not _private(t):
                hints[t] = Hint(t, conf, evidence)
    return sorted(hints.values(), key=lambda h: -h.confidence)


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
    matches = list(QUOTED_IDENT_RE.finditer(message))
    deprecated = list(
        dict.fromkeys(
            m.group("t")
            for m in matches
            if m.group("t") in params
            and (
                DEPRECATED_BEFORE_RE.search(message[: m.start()])
                or DEPRECATED_AFTER_RE.match(message[m.end() :])
            )
        )
    )
    if not deprecated:
        return {}
    evidence = message.strip()[:300]
    alternatives = [q for q in dict.fromkeys(m.group("t") for m in matches) if q not in deprecated]
    # Ties keep the message's own order ("Use 'proxy' or 'mounts'" -> proxy first).
    hints = [
        Hint(a, round((0.8 if a in params else 0.6) - 0.01 * i, 2), evidence)
        for i, a in enumerate(alternatives)
    ]
    return {p: list(hints) for p in deprecated}
