"""Mine "what to use instead" hints from deprecation text.

Deliberately conservative: a wrong replacement sends an agent down a dead end, so every hint carries the
evidence it came from and a confidence the caller can threshold. Patterns are pinned by tests.
"""

from __future__ import annotations

import re
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


def mine(message: str | None) -> list[Hint]:
    """Replacement candidates for a whole symbol, best first, deduplicated."""
    if not message:
        return []
    hints: dict[str, Hint] = {}
    evidence = message.strip()[:300]
    for m in LANGCHAIN_ALTERNATIVE_RE.finditer(message):
        hints.setdefault(_clean(m.group("t")), Hint(_clean(m.group("t")), 0.9, evidence))
    for pattern, conf in PATTERNS:
        for m in pattern.finditer(message):
            t = _clean(m.group("t"))
            if t and t.lower() not in STOPWORDS and t not in hints:
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
