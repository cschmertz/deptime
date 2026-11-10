"""Mine "what to use instead" hints from deprecation text.

Deliberately conservative: a wrong replacement sends an agent down a dead end, so every hint carries the
evidence it came from and a confidence the caller can threshold. Patterns are pinned by tests.
"""

from __future__ import annotations

import builtins
import keyword
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

# A name as deprecation text writes it: quoted any way (`x`, ``x``, 'x'), in a Sphinx role (:meth:`~x`), with a
# leading dot for a member of the same class ("Use .items instead"), or called ("select()").
_QUOTES = r"[`'\"]*"
_ROLE = r"(?P<role>:[a-z]+:)?"
# An import string, `module:name` (pydantic 2's `getattr_migration`: "has been moved to `pydantic:ValidationError`",
# DT-DATA-60), names `module.name`; not a URL's scheme ("docs:https://...").
_NAME = r"\.?[A-Za-z_][\w.]*(?::[A-Za-z_][\w.]*(?![\w.:]))?(?:\(\))?"
# In backticks, a name can be called or indexed, up to the closing backtick: "Use
# `matplotlib.colormaps.register(name)` instead", "`matplotlib.colormaps[name]`" (DT-DATA-31). The name is the target.
_CODE_ARGS = r"(?:(?:\([^`()]*(?:\([^`()]*\)[^`()]*)*\)|\[[^`\[\]]*\])(?=`))"
IDENT = rf"{_ROLE}(?P<bq>`+)?{_QUOTES}~?(?P<t>{_NAME})(?(bq){_CODE_ARGS}?){_QUOTES}"
IDENT_RE = re.compile(IDENT)
# After "the" a name is quoted or looks like code: "in favor of the Responses API" names an API (DT-CORE-23).
_THE = r"(?P<the>the\s+)?"
_CODE_SHAPED_RE = re.compile(r"[_.(\d]|[a-z][A-Z]")
PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(rf"\buse\s+{_THE}{IDENT}\s+instead", re.I), 0.85),
    (re.compile(rf"\b(?:moved|renamed)\s+to\s+{_THE}{IDENT}", re.I), 0.85),
    (re.compile(rf"\b(?:replaced|superseded)\s+(?:by|with)\s+{_THE}{IDENT}", re.I), 0.8),
    (re.compile(rf"\bin\s+favou?r\s+of\s+(?:using\s+)?{_THE}{IDENT}", re.I), 0.8),
    (re.compile(rf"\bnow\s+available\s+as\s+{IDENT}", re.I), 0.8),  # sqlalchemy's `became_legacy_20`
    # redis' "call hset instead", pandas' "check index.inferred_type instead" (DT-CORE-23).
    (
        re.compile(rf"\b(?:call|check|access|pass|using)\s+{_THE}{IDENT}\s+(?:directly\s+)?instead\b", re.I),
        0.8,
    ),
    # openai's "The `.content()` method should be used instead".
    (
        re.compile(
            rf"(?<![\w.]){IDENT}\s+(?:(?:method|function|class|attribute|property)\s+)?should\s+be\s+used\s+instead",
            re.I,
        ),
        0.8,
    ),
    # werkzeug's "Use feature detection or 'importlib.metadata.version("werkzeug")' instead".
    (
        re.compile(r"\bor\s+(?P<q>[`'\"]+)(?P<t>[A-Za-z_][\w.]*)(?:\([^()]*\))?(?P=q)\s+instead\b", re.I),
        0.8,
    ),
    # pydantic 2's "`pydantic.utils:deep_update` has been removed. We are importing from
    # `pydantic.v1.utils:deep_update` instead." (DT-DATA-60).
    (re.compile(rf"\bimporting\s+from\s+{IDENT}\s+instead\b", re.I), 0.8),
    (re.compile(rf"\bplease\s+use\s+{_THE}{IDENT}", re.I), 0.75),
    # transformers' "We recommend you using split_torch_state_dict_into_shards from huggingface_hub library"
    # (DT-CORE-40). "We recommend using native Keras" is prose (`_prose`).
    (
        re.compile(
            rf"\brecommend(?:s|ed)?\s+(?:(?:that\s+)?you\s+)?(?:to\s+)?us(?:e|ing)\s+{_THE}{IDENT}", re.I
        ),
        0.75,
    ),
    (re.compile(rf"\bcall\s+{_THE}{IDENT}\s+directly\b", re.I), 0.75),
    (re.compile(rf"\binstead,?\s+(?:use|call|access)\s+{_THE}{IDENT}", re.I), 0.75),
    (re.compile(rf"\buse\s+{_THE}{IDENT}", re.I), 0.6),
]
# sqlalchemy's "Please refer to Inspector.get_table_names()" (DT-CORE-40). "Refer to" often points at prose, a URL or
# a product ("refer to the documentation", "refer to https://…", langchain's "refer to LangGraph"), so only a name
# that is quoted, dotted, has an underscore or is called counts.
REFER_RE = re.compile(rf"\brefer\s+to\s+{_THE}{IDENT}", re.I)
REFER_CONFIDENCE = 0.75
# "Use `ChatMlflow` or `ChatDatabricks` instead", "Use DataFrame.ffill/Series.ffill instead": each name, in order.
# A slash joins names only without spaces: "Use GET /v2/audio/voices instead" is a URL.
_ITEM = rf"(?::[a-z]+:)?(?:`+~?{_NAME}{_CODE_ARGS}`+|{_QUOTES}~?{_NAME}{_QUOTES})"
LIST_RE = re.compile(
    rf"\buse\s+(?:the\s+)?(?P<list>{_ITEM}(?:\s*,\s*(?:or\s+|and\s+)?|/|\s+(?:or|and)\s+){_ITEM}"
    rf"(?:(?:\s*,\s*(?:or\s+|and\s+)?|/|\s+(?:or|and)\s+){_ITEM})*)\s+instead\b",
    re.I,
)
LIST_CONFIDENCE = 0.85
# langchain's "Please import from langchain-community instead: `from langchain_community.llms import OpenAI`".
IMPORT_RE = re.compile(
    r"\bfrom\s+(?P<mod>[A-Za-z_][\w.]*)\s+import\s+\(?\s*(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)"
)
IMPORT_CONFIDENCE = 0.8
# "# Old way from instructor import from_gemini … # New way from instructor import from_genai".
_OLD_NEW_RE = re.compile(r"\b(old|new|before|after)\b", re.I)
# A bare lowercase word that more prose follows is English: "use case documentation", "use new agent methods".
_WORD_RE = re.compile(r"[a-z]+")
_FOLLOWING_WORD_RE = re.compile(r"\s+[`'\"]?([A-Za-z]+)")
_NOT_PROSE_AFTER = {"instead", "or", "and", "directly"}
# pandas' "use DataFrame/Series.shift": a class, then a slash and the member both share.
_SHORTHAND_RE = re.compile(r"[`'\"]*/[`'\"]*[A-Za-z_]\w*\.(?P<leaf>[A-Za-z_]\w*)")
# "`BaseSettings` has been moved to the `pydantic-settings` package": the target is a distribution (DT-CORE-06).
PACKAGE_MOVE_RE = re.compile(
    r"\bmoved\s+to\s+the\s+[`'\"]?(?P<dist>[A-Za-z0-9][\w.-]*)[`'\"]?\s+(?:package|distribution|library)\b",
    re.I,
)
# "use X from huggingface_hub library": a bare name in another distribution, by its import name (DT-CORE-40).
FROM_LIBRARY_RE = re.compile(
    r"[`'\"]*\s+from\s+(?:the\s+)?[`'\"]?(?P<mod>[A-Za-z_]\w*)[`'\"]?\s+(?:library|package|module)\b", re.I
)
MAX_EVIDENCE_CHARS = 300
QUOTED_IDENT_RE = re.compile(r"['`\"](?P<t>[A-Za-z_]\w*)['`\"]")
# pandas' "Use obj.ffill() or obj.bfill() instead": `obj.` is the object the method is called on, so the target is
# the class's own member, written with a leading dot as other same-class targets are (DT-CORE-44).
OBJ_RECEIVER_RE = re.compile(r"obj\.(?=[A-Za-z_]\w*$)")
# Not a chain or a call with arguments, which are expressions: "use obj.T.groupby(...)", "`obj = obj.set_axis(...)`".
OBJ_METHOD_RE = re.compile(r"(?<![\w.])[`'\"]?obj\.(?P<t>[A-Za-z_]\w*)(?:\(\))?(?![\w.(])[`'\"]?")
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
    "which",
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
# log_output", "in favor of `.condition`", "Use `verify=<ssl_context>` instead", "Use the lifespan parameter
# instead", "Use ``sep='\s+'`` instead" (DT-CORE-23).
PARAM_CUE_RE = re.compile(
    r"\b(?:use|using|pass|favou?r\s+of|replaced\s+(?:by|with)|renamed(?:\s+to)?)\s+(?:the\s+)?[`'\"]*\.?"
    r"(?P<t>[A-Za-z_]\w*)",
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
    return target.removesuffix("()").strip(".`'\" ").replace(":", ".", 1)


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


def _prose(message: str, start: int, end: int) -> bool:
    """Whether the name at message[start:end] is a word of a sentence: bare, unquoted, lowercase, and followed by
    another word ("its use is discouraged", "use feature detection"), not by "instead" or punctuation."""
    if not _WORD_RE.fullmatch(message[start:end]) or (start and message[start - 1] in "`'\"."):
        return False
    follow = _FOLLOWING_WORD_RE.match(message, end)
    return bool(follow) and follow.group(1).lower() not in _NOT_PROSE_AFTER


def _target(message: str, m: re.Match[str], skip_expressions: bool, group: str = "t") -> str | None:
    """The name a match names, or None if it isn't one: a word, a keyword, private, a distribution, a name the
    snapshot couldn't read to the end, or (with `skip_expressions`) the head of an expression.

    A leading dot stays, for the resolver to read as the deprecated name's own class (DT-CORE-23), except in a
    Sphinx role, where it means "look it up" and `_alias.` is a doc alias of a module (sqlalchemy's
    :meth:`_engine.Engine.begin`)."""
    start, end = m.span(group)
    after = message[end : end + 2]
    if after[:1] == "…" or (skip_expressions and after[:1] in ("(", "[", ".")):
        return None  # "Use pandas.api.typing.… instead": the snapshot couldn't read the rest of the name
    if after[:1] == "-" and after[1:].isalnum():
        return None  # "use langchain-nvidia-ai-endpoints", "use built-in …": a distribution or a word
    raw = m.group(group)
    if m.re.groupindex.get("role") and m.group("role"):
        raw = re.sub(r"^\.?_[a-z]+\.(?=.)", "", raw).lstrip(".")
    t = ("." if raw.startswith(".") else "") + _clean(raw)
    if OBJ_RECEIVER_RE.match(t):  # "Use obj.ffill() instead": the object it's called on (DT-CORE-44)
        t = t[len("obj") :]
    bare = t.lstrip(".")
    if not bare or bare.lower() in STOPWORDS or keyword.iskeyword(bare) or _private(bare):
        return None
    if "." not in t and (shorthand := _SHORTHAND_RE.match(message, end)):
        t = f"{t}.{shorthand.group('leaf')}"
    elif "." not in t and (library := FROM_LIBRARY_RE.match(message, end)):
        t = f"{library.group('mod')}.{t}"
    marked = start > 0 and message[start - 1] in "`'\"~"
    if m.re.groupindex.get("the") and m.group("the") and not marked and not _CODE_SHAPED_RE.search(raw):
        return None
    return None if _prose(message, start, end) else t


def _imports(message: str) -> Iterable[str]:
    """`module.name` for each name a `from module import name` statement imports as advice: not one the wording
    deprecates ("`from x import y` is deprecated") or that an "Old way" introduces."""
    for m in IMPORT_RE.finditer(message):
        if DEPRECATED_AFTER_RE.match(message[m.end() :].lstrip("`'\"")):
            continue
        cue = [w.group(1).lower() for w in _OLD_NEW_RE.finditer(message, max(0, m.start() - 40), m.start())]
        if cue and cue[-1] in ("old", "before"):
            continue
        for name in re.split(r"\s*,\s*", m.group("names")):
            target = f"{m.group('mod')}.{name}"
            if not _private(target):
                yield target


def mine(message: str | None, *, skip_expressions: bool = False) -> list[Hint]:
    """Replacement candidates for a whole symbol, best first, deduplicated.

    `skip_expressions` drops a target that is only the head of a longer expression: `np.dtype` in "Use
    `np.dtype(obj).type` instead", `arr` in "Use `arr[index] = value` instead". A target can start with a dot
    (".items"): a member of the deprecated name's class, which `PublicNames.resolve` reads.
    """
    if not message:
        return []
    hints: dict[str, Hint] = {}
    evidence = message.strip()[:300]

    def add(t: str | None, conf: float) -> None:
        if t and (t not in hints or hints[t].confidence < conf):
            hints[t] = Hint(t, conf, evidence)

    for m in LIST_RE.finditer(message):
        items = IDENT_RE.finditer(message, m.start("list"), m.end("list"))
        names = [item for item in items if item.group("t").lower() not in ("or", "and")]
        for i, item in enumerate(names):
            add(_target(message, item, skip_expressions), round(LIST_CONFIDENCE - 0.01 * i, 2))
    for pattern, conf in PATTERNS:
        for m in pattern.finditer(message):
            add(_target(message, m, skip_expressions), conf)
    for m in REFER_RE.finditer(message):
        start = m.start("t")
        opened = bool(m.group("bq")) or (start > 0 and message[start - 1] in "`'\"")
        # Quoted means closed after the name: "refer to the `LangGraph documentation <https://…>`_" is a link.
        marked = bool(m.group("role")) or (opened and message[m.end() - 1] in "`'\"")
        raw = m.group("t")  # "refer to docs." isn't code: the sentence's full stop doesn't count
        if marked or raw.endswith("()") or re.search(r"[._]", _clean(raw)):
            add(_target(message, m, skip_expressions), REFER_CONFIDENCE)
    for t in _imports(message):
        add(t, IMPORT_CONFIDENCE)
    return sorted(hints.values(), key=lambda h: -h.confidence)


# "using `EvaluationStrategy` for `eval_strategy` is deprecated", "Passing a tuple of `past_key_values` is
# deprecated": the name is what something else is passed as, and what is deprecated is that way of passing it, not
# the name. Not "Use of `x`" or "Support for `x`", whose subject is `x` (DT-CORE-40).
_PASSED_AS_RE = re.compile(
    r"\b(?:using|passing|setting|providing|specifying|giving)\s+(?:[^.!?;]|\.(?!\s))+?\s(?:for|of|as|to|in)\s+"
    r"(?:the\s+|a\s+|an\s+)?[`'\"*]*$",
    re.I,
)


def _deprecated_at(message: str, start: int, end: int) -> bool:
    if DEPRECATED_BEFORE_RE.search(message[:start]):
        return True
    if not DEPRECATED_AFTER_RE.match(message[end:]):
        return False
    return not _PASSED_AS_RE.search(message[_sentence_start(message, start) : start])


def passed_as(message: str, name: str) -> bool:
    """Whether the wording deprecates a way of passing `name`, quoted: "using a string relationship name for the
    "onclause" is deprecated" is about a usage of `onclause`, so neither it nor the function is deprecated."""
    for m in re.finditer(rf"[`'\"*]+{re.escape(name)}[`'\"*]+", message):
        if DEPRECATED_AFTER_RE.match(message[m.end() :]) and _PASSED_AS_RE.search(
            message[_sentence_start(message, m.start()) : m.start() + 1]
        ):
            return True
    return False


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


_CLASS_DEPRECATED_RE = re.compile(
    r"[`'\"*]*\s+class\s+(?:is|was|has\s+been|will\s+be)\s+(?:now\s+)?deprecated", re.I
)


def deprecates_class(message: str, name: str) -> bool:
    """Whether the wording deprecates the class `name`: as `deprecates_name` does ("The class `X` is deprecated"),
    or with the noun after it ("The `X` class is deprecated")."""
    return deprecates_name(message, name) or any(
        _CLASS_DEPRECATED_RE.match(message, m.end())
        for m in re.finditer(rf"[`'\"*]+{re.escape(name)}\b", message)
    )


def _sentence_start(message: str, at: int) -> int:
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(message, 0, at)]
    return ends[-1] if ends else 0


_QUOTE = r"[)\]`'\"*]*"
_OTHER_NAMES = (
    r"(?:[`'\"*]*\w+[`'\"*]*\s*(?:,|\band\b|\bor\b)\s*)*"  # "The on_startup and on_shutdown parameters"
)
_ARGUMENT_BEFORE_RE = re.compile(rf"^\s*(?:the\s+)?(?:{_PARAM_NOUN}s?\s+)?{_OTHER_NAMES}{_QUOTE}$", re.I)
_ARGUMENT_AFTER_RE = re.compile(
    rf"^{_QUOTE}\s*(?:(?:,|and|or)\s+[`'\"*]*\w+[`'\"*]*\s*)*(?:{_PARAM_NOUN}s?\s+)?(?:(?:in|of|to|for|on)\s+\S+\s+)?"
    r"(?:is|are|was|were|has\s+been|have\s+been|will\s+be)\s+(?:now\s+)?(?:being\s+)?"
    r"(?:deprecated|removed|no\s+longer\s+supported)",
    re.I,
)


def deprecates_argument(message: str, name: str) -> bool:
    """Whether a sentence of `message` deprecates the argument `name` itself, as its subject (DT-DATA-39): "`regex`
    has been deprecated", "The on_startup and on_shutdown parameters are deprecated", "The 'method' keyword in
    DataFrame.fillna is deprecated", or the argument with any value, "`cert=...` is deprecated". Not a value or a
    way of calling: "Using dict as a `filter` is deprecated", "Omitting `issuer` is deprecated", "parameter
    'timeout' of type 'float' is deprecated"."""
    for m in re.finditer(rf"\b{re.escape(name)}\b(?P<any>=(?:\.\.\.|<\.\.\.>))?", message):
        if not _ARGUMENT_AFTER_RE.match(message[m.end() :]):
            continue
        before = message[_sentence_start(message, m.start()) : m.start()]
        if m.group("any") or _ARGUMENT_BEFORE_RE.match(before):
            return True
    return False


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


# "You will need to pass 'theme' to Blocks.launch() instead" (gradio 5.50), "Pass the parameter `pdp_lim` to
# sklearn.inspection.PartialDependenceDisplay.plot() instead": the same argument, given to another callable.
KWARG_EXPR_RE = re.compile(r"(?P<q>[`'\"]+)(?P<t>[A-Za-z_]\w*)=(?:(?!(?P=q)).)+(?P=q)")
PASS_TO_RE = re.compile(
    rf"\bpass\s+(?:the\s+{_PARAM_NOUN}\s+)?{_QUOTES}(?P<p>\w+){_QUOTES}\s+to\s+{_QUOTES}"
    rf"(?P<f>[A-Za-z_][\w.]*)\(\){_QUOTES}\s+instead\b",
    re.I,
)


def param_alternatives(message: str, deprecated: list[str], params: list[str]) -> list[Hint]:
    """What to pass instead of the `deprecated` arguments: every other quoted name, and parameters named after a
    cue ("please use log_output", "in favor of `.condition`"), in the message's order. A parameter of the
    function scores 0.8 when its sentence has a cue before it ("Use 'proxy' or 'mounts' instead") or "instead"
    after it ("Please configure the 'retry' object instead"), anything else 0.6: polars' "…only be included if `k`
    is larger…" mentions `k` in passing (DT-CORE-12). Ties keep the message's order. First, at 0.8, another callable
    to pass the same argument to ("pass 'theme' to Blocks.launch() instead" gives `Blocks.launch(theme)`)."""
    # A keyword argument written out in quotes after a cue: gradio 5.50's "use 'buttons=["copy"]' instead" advises
    # `buttons`, a parameter of a later release, and the names in its value aren't arguments (DT-CORE-40). A method of
    # the object, written `obj.X` ("Use obj.ffill() or obj.bfill() instead"), is `.X`, the class's own (DT-CORE-44).
    expressions = list(KWARG_EXPR_RE.finditer(message))
    written = [
        m
        for m in expressions
        if CUE_BEFORE_RE.search(message[: m.start()]) or INSTEAD_AFTER_RE.match(message, m.end())
    ]
    inside = [m.span() for m in expressions]
    found = [
        (m.start(), m.end(), m.group("t"))
        for m in QUOTED_IDENT_RE.finditer(message)
        if not any(a <= m.start() and m.end() <= b for a, b in inside)
    ]
    found += [
        (m.start("t"), m.end("t"), m.group("t"))
        for m in PARAM_CUE_RE.finditer(message)
        if m.group("t") in params
    ]
    found += [(m.start("t"), m.end("t"), m.group("t")) for m in written]
    receiver = [(m.start(), m.end(), "." + m.group("t")) for m in OBJ_METHOD_RE.finditer(message)]
    found += receiver
    cued = (
        {m.group("t") for m in written}
        | {
            n
            for at, end, n in receiver
            if CUE_BEFORE_RE.search(message[:at]) or INSTEAD_AFTER_RE.match(message, end)
        }
        | {
            n
            for at, end, n in found
            if n in params and (CUE_BEFORE_RE.search(message[:at]) or INSTEAD_AFTER_RE.match(message, end))
        }
    )
    names = [n for n in dict.fromkeys(n for _, _, n in sorted(found)) if n not in deprecated]
    evidence = message.strip()[:300]
    moved = [
        Hint(f"{m.group('f')}({m.group('p')})", 0.8, evidence)
        for m in PASS_TO_RE.finditer(message)
        if m.group("p") in deprecated
    ]
    return moved + [
        Hint(n, round((0.8 if n in cued else 0.6) - 0.01 * i, 2), evidence) for i, n in enumerate(names)
    ]


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


INSTANCE_NAMES = ("self", "obj")


def same_class(target: str, path: str) -> str | None:
    """The member of `path`'s own class that `target` names, if it names one that way (DT-CORE-23): ".items" on
    `pandas.DataFrame.iteritems`, `self.x` or `obj.x`, or an instance named after the class (`index.inferred_type`
    on `pandas.Index.is_all_dates`, or on IntervalIndex's)."""
    owner = path.rpartition(".")[0]
    if not owner:
        return None
    if target.startswith("."):
        return owner + target
    head, _, rest = target.partition(".")
    cls = owner.rpartition(".")[2].lower()
    if rest and (head in INSTANCE_NAMES or cls == head or (len(head) >= 3 and cls.endswith(head))):
        return f"{owner}.{rest}"
    return None


class PublicNames:
    """One package's public paths as its index rows have them, to check what a hint names (DT-CORE-12).

    `spans` are (path, last indexed ordinal, object): the object is the path's canonical definition, or the path
    itself for a name a `__getattr__` serves (they all share its canonical). `deprecations` are (path, first
    ordinal, last ordinal, message) of the paths deprecated as a whole. `instances` are the paths of attributes:
    objects whose members the index doesn't record (DT-DATA-31). `members` gives the last ordinal and object of a
    class member `spans` doesn't list, for a package stored with own members (DT-DATA-16): an inherited one; `heirs`
    gives, for a member `spans` lists, the paths of the public classes that inherit it.
    """

    def __init__(
        self,
        import_names: Sequence[str],
        spans: Iterable[tuple[str, int, str | None]],
        deprecations: Iterable[tuple[str, int, int, str]] = (),
        instances: Iterable[str] = (),
        members: Callable[[str], tuple[int, str] | None] | None = None,
        heirs: Callable[[str], list[str]] | None = None,
    ):
        self.import_names = tuple(import_names)
        self._instances = frozenset(instances)
        self._last: dict[str, int] = {}
        self._object: dict[str, str] = {}
        for path, last, obj in spans:
            if last >= self._last.get(path, -1):
                self._last[path], self._object[path] = last, obj or path
        self._members = members
        self._heirs = heirs
        self._inherited: dict[str, tuple[int, str] | None] = {}
        self._tails: dict[str, list[str]] | None = None
        self._deprecated: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for path, first, last, message in deprecations:
            self._deprecated[path].append((first, last, message))

    def _deprecation(self, path: str, at: int) -> str | None:
        return next((m for first, last, m in self._deprecated.get(path, ()) if first <= at <= last), None)

    def _info(self, path: str) -> tuple[int, str | None]:
        """`path`'s last ordinal and object; -1 and None when the package never had it. For a class member, the
        later of its rows and its lookup: pandas 1.5's `DataFrame` defines `ffill`, and from 2.0 it inherits it, so
        "Use DataFrame.ffill" on 2.0's `pad` named a path its rows end at 1.5 (DT-DATA-49)."""
        own = (self._last[path], self._object[path]) if path in self._last else None
        if self._members is None:
            return own or (-1, None)
        if path not in self._inherited:
            self._inherited[path] = self._members(path)
        inherited = self._inherited[path]
        if own is None or inherited is None:
            return own or inherited or (-1, None)
        return inherited if inherited[0] > own[0] else own

    def _itself(self, target: str, path: str, since: int) -> bool:
        """`target` is the deprecated object at another path, deprecated there in the same words: langchain-
        community's "Please use `HanaDB` from the langchain_hana package" found on its own re-export (DT-CORE-23).
        numpy 2's `numpy.linalg.det` for `numpy.linalg.linalg.det` isn't deprecated, so it is where to import it."""
        message = self._deprecation(path, since)
        return (
            message is not None
            and self._info(target)[1] == self._info(path)[1]
            and self._deprecation(target, since) == message
        )

    def _own(self, target: str) -> bool:
        return any(target == n or target.startswith(n + ".") for n in self.import_names)

    def _found(self, candidate: str, path: str, since: int) -> bool:
        """`candidate` is public at release `since` or later. It can be the deprecated object at another path:
        numpy 2's `numpy.linalg.linalg.det` says to import `numpy.linalg.det`."""
        return self._info(candidate)[0] >= since and candidate != path

    def _by_tail(self, name: str, path: str, since: int) -> list[str]:
        """Paths ending in `name` public at `since` or later, other than the deprecated object itself: a bare
        `product` doesn't mean `numpy.core.fromnumeric.product` on `numpy.product`."""
        if self._tails is None:
            self._tails = defaultdict(list)
            for p in self._last:
                self._tails[p.rpartition(".")[2]].append(p)
        same = self._info(path)[1] or path
        found = [
            p for p in self._tails.get(name, ()) if self._found(p, path, since) and self._object[p] != same
        ]
        if self._heirs is not None:
            # With own members, a class's inherited members aren't rows: "use `hset`" on redis meant `Redis.hset`,
            # inherited from `HashCommands` (DT-DATA-49).
            found += [q for p in found for q in self._heirs(p) if self._found(q, path, since)]
        return list(dict.fromkeys(found))

    def _one(self, paths: list[str]) -> str | None:
        """The shortest of `paths` if they're all one object (re-exports of it), else None."""
        if len({self._info(p)[1] for p in paths}) != 1:
            return None
        return min(paths, key=lambda p: (p.count("."), len(p), p))

    def resolve(self, target: str, path: str, since: int) -> tuple[str, bool | None]:
        """(target, verdict) for a hint on `path` from a deprecation first seen at ordinal `since`.

        True: the index has it; the target comes back as the public path it means (`prod` on `numpy.product` ->
        `numpy.prod`), or as written if several objects have that name. False: the index contradicts it, a path in
        the package's own namespace it doesn't have, or a bare name no indexed release has. None: it's outside
        the package (another distribution, the standard library, builtins, `name.map` on an instance, a member of
        an object the package binds as an attribute: `matplotlib.colormaps.register`), or a bare name only earlier
        releases have, so the index can't say.

        Tried in order: a member of `path`'s class, if the target names one that way (`same_class`); as written,
        if it's in the package's namespace (else, for a dotted name, the same leaf next to `path`); under each of
        `path`'s ancestors, nearest first, and the package's top level; then a bare name anywhere in the package,
        or a dotted one under any object named like its head.
        """
        target = expand_alias(target)
        if member := same_class(target, path):
            if self._found(member, path, since):
                return member, True
            head, _, rest = target.lstrip(".").partition(".")
            found, verdict = self._resolve(
                rest if head in INSTANCE_NAMES and rest else target.lstrip("."), path, since
            )
            # A member the index can't check (polars' `.str.explode()`) keeps the dot that says whose it is.
            return (target, None) if verdict is None and target.startswith(".") else (found, verdict)
        return self._resolve(target, path, since)

    def _resolve(self, target: str, path: str, since: int) -> tuple[str, bool | None]:
        if self._own(target):
            if self._found(target, path, since):
                return target, True
            sibling = f"{path.rpartition('.')[0]}.{target.rpartition('.')[2]}"
            if "." in target and self._found(sibling, path, since):
                return sibling, True
            if any(a in self._instances and self._found(a, path, since) for a in _ancestors(target)):
                return target, None
            return target, False
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
        if verdict and self._itself(target, path, since):
            verdict = False
        confidence = (
            hint.confidence if verdict is not False else min(hint.confidence, CONTRADICTED_CONFIDENCE)
        )
        return Hint(target, confidence, hint.evidence)
