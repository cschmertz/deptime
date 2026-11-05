"""Parse Python tracebacks (or a bare exception line) into a structured error signal.

Deterministic and dependency-free on purpose: this runs on untrusted input on every `explain_error`
call. Supported message shapes are pinned by tests/test_tracebacks.py; extend there first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from deptime.models import ErrorSignalOut, FrameOut

MAX_INPUT_CHARS = 20_000  # analysed from the end of the input
MAX_ACCEPTED_CHARS = 200_000  # the MCP tool's input limit; the parser itself keeps the last MAX_INPUT_CHARS

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Column-0 prefixes that wrap whole lines: pytest's "E   " and "INTERNALERROR> ", and a compose service's "api-1  | ".
LINE_PREFIX_RE = re.compile(r"^(?:E\s+|INTERNALERROR>\s?|[\w.-]+\s*\|\s?)")
# The bars an ExceptionGroup (3.11+) draws in front of each sub-exception's lines, one per nesting level.
GROUP_BAR_RE = re.compile(r"^\s*\|(?: |$)")
TRACEBACK_HEADER = "Traceback (most recent call last):"
# The lines Python prints between a chained exception and the one it caused (DT-SPIKE-03).
CHAIN_MARKERS = {
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
}
FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+?))?\s*$')
EXC_LINE_RE = re.compile(  # openai's APIRemovedInV1 is the one supported exception without the usual suffix
    r"^(?P<type>(?:[A-Za-z_]\w*\.)*(?:[A-Za-z_]\w*)?(?:Error|Exception|APIRemovedInV1))(?::\s?(?P<msg>.*))?$"
)
MAX_MESSAGE_LINES = 10  # lines after the exception line that may continue a multi-line message
CARET_RE = re.compile(r"^\s*[~^]+\s*$")
SITE_RE = re.compile(r"[\\/](?:site|dist)-packages[\\/](?P<rel>.+?)\.pyi?$")
SUGGESTION_RE = re.compile(r"\.?\s*Did you mean:? '(?P<s>[\w.]+)'\??\s*$")
DOTTED_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)")
CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)\s*\(")
FROM_IMPORT_RE = re.compile(r"^\s*from\s+(?P<mod>[\w.]+)\s+import\s+(?P<names>[\w\s,()]+)")

CIRCULAR_RE = re.compile(r"partially initialized module '(?P<module>[\w.]+)'")
IMPORT_NAME_RE = re.compile(r"cannot import name '(?P<name>[\w.]+)' from '(?P<module>[\w.]+)'")
BARE_IMPORT_NAME_RE = re.compile(r"cannot import name '(?P<name>\w+)'\s*$")
NO_MODULE_RE = re.compile(r"No module named '(?P<module>[\w.]+)'")
MODULE_ATTR_RE = re.compile(r"module '?(?P<module>[\w.]+)'? has no attribute '?(?P<name>\w+)'?")
TYPE_ATTR_RE = re.compile(r"type object '(?P<owner>[\w.]+)' has no attribute '(?P<name>\w+)'")
OBJ_ATTR_RE = re.compile(r"'(?P<owner>[\w.]+)' object has no attribute '(?P<name>\w+)'")
UNEXPECTED_KW_RE = re.compile(r"(?P<owner>[\w.]+)\(\) got an unexpected keyword argument '(?P<kwarg>\w+)'")
BARE_KW_RE = re.compile(r"got an unexpected keyword argument '(?P<kwarg>\w+)'")  # no callable named
MISSING_ARG_RE = re.compile(
    r"(?P<owner>[\w.]+)\(\) missing \d+ required (?:positional|keyword-only) arguments?: (?P<names>.+)$"
)
TOO_MANY_POS_RE = re.compile(
    r"(?P<owner>[\w.]+)\(\) takes .+? positional arguments? but \d+ (?:were|was) given"
)
# Libraries that name a removed API in their own words (DT-CORE-05); templates copied from their wheels.
NUMPY_EXPIRED_RE = re.compile(r"`np\.(?P<name>\w+)` was removed in the NumPy 2\.0 release")
# numpy 2.0.0-2.3.5 only; 2.4.0 raises the standard "'numpy.ndarray' object has no attribute" message (DT-CORE-21).
NUMPY_MEMBER_REMOVED_RE = re.compile(
    r"`(?P<name>\w+)` was removed from (?P<owner>the ndarray class|scalar types) in NumPy 2\.0"
)
NUMPY_MEMBER_OWNERS = {"the ndarray class": "numpy.ndarray", "scalar types": "numpy.generic"}
PYDANTIC_REMOVED_RE = re.compile(r"`(?P<module>[\w.]+):(?P<name>\w+)` has been removed in V2")
PYDANTIC_MOVED_RE = re.compile(r"`(?P<name>BaseSettings)` has been moved to the `pydantic-settings` package")
PYDANTIC_REMOVED_KWARG_RE = re.compile(r"`(?P<kwarg>\w+)` is removed[.,] use ")
OPENAI_V1_RE = re.compile(r"You tried to access openai\.(?P<name>\w+), but this is no longer supported")
# huggingface_hub's lazy loader, worded like scientific-python's lazy_loader: "No huggingface_hub attribute x".
LAZY_ATTR_RE = re.compile(r"^No (?P<module>[A-Za-z_][\w.]*) attribute (?P<name>\w+)$")
HOOK_ARG_RE = re.compile(
    r"for hook '(?P<hook>\w+)'.*?Argument\(s\) \{'(?P<arg>\w+)'.*? are declared in the hookimpl", re.S
)
# The exception type a message implies, for errors logged or quoted without their exception line.
INFERRED_TYPE = {
    "circular_import": "ImportError",
    "import_name": "ImportError",
    "module_missing": "ModuleNotFoundError",
    "module_attribute": "AttributeError",
    "class_attribute": "AttributeError",
    "object_attribute": "AttributeError",
    "unexpected_kwarg": "TypeError",
    "missing_argument": "TypeError",
    "too_many_positional": "TypeError",
}


@dataclass
class Frame:
    file: str
    line: int
    function: str
    module: str | None = None  # dotted module path when the file lives in site-packages
    code: str | None = None


@dataclass
class ErrorSignal:
    kind: str  # see ErrorSignalOut.kind; "unknown" when no supported pattern matched
    exception_type: str
    message: str
    module: str | None = None
    name: str | None = None
    owner: str | None = None
    kwarg: str | None = None
    missing_args: list[str] = field(default_factory=list)
    suggestion: str | None = None
    referenced_names: list[str] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    # The exception type printed last when it only wraps this one (`raise X from exc`, or raised while handling it).
    wrapped_in: str | None = None
    # Owners an unexpected keyword argument may really belong to: the class the caller constructed, whose
    # `__init__` passed `**kwargs` on to the parent the message names (DT-CORE-27).
    forwarded_owners: list[str] = field(default_factory=list)

    @property
    def library_modules(self) -> list[str]:
        """Dotted modules of site-packages frames, innermost last (deduplicated, order kept)."""
        seen: dict[str, None] = {}
        for f in self.frames:
            if f.module:
                seen.setdefault(f.module, None)
        return list(seen)

    def to_model(self) -> ErrorSignalOut:
        return ErrorSignalOut(
            kind=self.kind,
            exception_type=self.exception_type,
            message=self.message,
            module=self.module,
            name=self.name,
            owner=self.owner,
            kwarg=self.kwarg,
            missing_args=self.missing_args,
            suggestion=self.suggestion,
            referenced_names=self.referenced_names,
            frames=[
                FrameOut(file=f.file, line=f.line, function=f.function, module=f.module, code=f.code)
                for f in self.frames
            ],
        )


def _clean_lines(text: str) -> list[str]:
    # Keep the tail: Python prints the exception line last, after any amount of log output.
    text = ANSI_RE.sub("", text[-MAX_INPUT_CHARS:])
    return [_unprefixed(line.rstrip("\r")) for line in text.splitlines()]


def _unprefixed(line: str) -> str:
    line = LINE_PREFIX_RE.sub("", line)
    while m := GROUP_BAR_RE.match(line):
        line = line[m.end() :]
    return line


def _exception_text(line: str) -> str:
    """The line as an exception line reads: without indentation or a leading comment marker."""
    return line.strip().lstrip("#").lstrip()


def _module_from_path(path: str) -> str | None:
    m = SITE_RE.search(path)
    if not m:
        return None
    parts = re.split(r"[\\/]", m.group("rel"))
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _referenced_names(code_lines: list[str]) -> list[str]:
    found: dict[str, None] = {}
    for code in code_lines:
        fm = FROM_IMPORT_RE.match(code)
        if fm:
            for n in re.findall(r"\w+", fm.group("names")):
                found.setdefault(f"{fm.group('mod')}.{n}", None)
        for dotted in DOTTED_RE.findall(code):
            found.setdefault(dotted, None)
    return list(found)


def parse_traceback(text: str) -> ErrorSignal | None:
    """Return the signal for the *last* exception in `text`, or None if no exception line is found.

    When that exception only wraps another (a chain: `raise X from exc`, or an exception raised while handling one)
    and isn't a shape DepTime analyses itself, the nearest exception down the chain that is one is returned
    instead, with `wrapped_in` naming the last one."""
    lines = _clean_lines(text)

    found = [
        (i, m.group("type"), (m.group("msg") or "").strip())
        for i, line in enumerate(lines)
        if (m := EXC_LINE_RE.match(_exception_text(line)))
    ]
    if not found:
        # Logged, quoted or JSON-wrapped errors lose their exception line: use the last known message.
        for i in range(len(lines) - 1, -1, -1):
            probe = ErrorSignal(kind="unknown", exception_type="", message="")
            _classify(probe, lines[i])
            if probe.kind != "unknown":
                return _signal_at(lines, i, INFERRED_TYPE[probe.kind], lines[i].strip())
        return None

    signal = _signal_at(lines, *found[-1])
    last = signal.exception_type
    for (cause, *rest), (effect, _, _) in zip(reversed(found[:-1]), reversed(found[1:]), strict=True):
        if signal.kind != "unknown" or not any(
            line.strip() in CHAIN_MARKERS for line in lines[cause + 1 : effect]
        ):
            break
        signal = _signal_at(lines, cause, *rest)
        signal.wrapped_in = last if signal.kind != "unknown" else None
    if signal.kind == "unknown":
        return _signal_at(lines, *found[-1])
    return signal


def _signal_at(lines: list[str], exc_index: int, exc_type: str, exc_msg: str) -> ErrorSignal:
    """The signal for the exception printed at `lines[exc_index]`, with the frames of its own traceback block."""
    start = 0
    for i in range(exc_index, -1, -1):
        if lines[i].strip() == TRACEBACK_HEADER:
            start = i
            break
    frames: list[Frame] = []
    for i in range(start, exc_index):
        fm = FRAME_RE.match(lines[i])
        if not fm:
            continue
        code = None
        if i + 1 < exc_index:
            nxt = lines[i + 1]
            if (
                nxt.strip()
                and not FRAME_RE.match(nxt)
                and not CARET_RE.match(nxt)
                and not EXC_LINE_RE.match(_exception_text(nxt))
            ):
                code = nxt.strip()
        frames.append(
            Frame(
                file=fm.group("file"),
                line=int(fm.group("line")),
                function=(fm.group("func") or "<module>").strip(),
                module=_module_from_path(fm.group("file")),
                code=code,
            )
        )

    suggestion = None
    sm = SUGGESTION_RE.search(exc_msg)
    message_core = exc_msg
    if sm:
        suggestion = sm.group("s")
        message_core = exc_msg[: sm.start()].rstrip(" .")

    signal = ErrorSignal(
        kind="unknown",
        exception_type=exc_type.rsplit(".", 1)[-1],
        message=exc_msg,
        suggestion=suggestion,
        frames=frames,
        referenced_names=_referenced_names([f.code for f in frames if f.code]),
    )
    continuation = lines[exc_index + 1 : exc_index + 1 + MAX_MESSAGE_LINES]
    _classify_library(signal, "\n".join([message_core, *continuation]))
    if signal.kind == "unknown":
        _classify(signal, message_core)
    if signal.kind == "unexpected_kwarg" and signal.owner is None:
        _owner_from_call_site(signal, lines)
    if signal.owner == "__init__" and signal.kind in {
        "unexpected_kwarg",
        "missing_argument",
        "too_many_positional",
    }:
        _owner_from_constructor_call(signal)
    if signal.kind == "unexpected_kwarg":
        _owner_forwarded_kwargs(signal)
    return signal


def _owner_forwarded_kwargs(signal: ErrorSignal) -> None:
    """An `__init__` that passes `**kwargs` on to its parent raises in the parent's name (DT-CORE-27): gradio 6's
    `gr.Blocks(theme=...)` fails as `BlockContext.__init__() got an unexpected keyword argument 'theme'`. The
    class the frame before it calls (a CapWords name) may have taken that argument once."""
    frames = [f for f in signal.frames if f.code]
    if len(frames) < 2 or frames[-1].function != "__init__" or "**" not in (frames[-1].code or ""):
        return
    calls = [c for c in CALL_RE.findall(frames[-2].code or "") if c.rsplit(".", 1)[-1][:1].isupper()]
    if not calls:
        return
    owner = f"{calls[-1].rsplit('.', 1)[-1]}.__init__"
    if owner != signal.owner and owner not in signal.forwarded_owners:
        signal.forwarded_owners.append(owner)
        if calls[-1] not in signal.referenced_names:
            signal.referenced_names.append(calls[-1])


def _owner_from_constructor_call(signal: ErrorSignal) -> None:
    """Python 3.9 and earlier say `__init__() got an unexpected keyword argument 'x'` with no class (DT-CORE-26):
    take the class the raising frame calls (`client = genai.GenerativeModel(`), a CapWords name."""
    code = next((f.code for f in reversed(signal.frames) if f.code), None)
    calls = [c for c in CALL_RE.findall(code or "") if c.rsplit(".", 1)[-1][:1].isupper()]
    if calls:
        signal.owner = f"{calls[-1].rsplit('.', 1)[-1]}.__init__"
        if calls[-1] not in signal.referenced_names:
            signal.referenced_names.append(calls[-1])


def _owner_from_call_site(signal: ErrorSignal, lines: list[str]) -> None:
    """`got an unexpected keyword argument 'x'` with no callable (scikit-learn): take the call that passes x=."""
    call = re.compile(rf"(?P<callee>[A-Za-z_][\w.]*)\([^()]*\b{re.escape(signal.kwarg or '')}\s*=")
    for line in reversed(lines):
        if m := call.search(line):
            callee = m.group("callee")
            signal.owner = callee.rsplit(".", 1)[-1]
            if callee not in signal.referenced_names:
                signal.referenced_names.append(callee)
            return


def _classify_library(signal: ErrorSignal, text: str) -> None:
    """Migration errors raised by the library itself, matched by exception class. `text` is the message and the
    lines after it, where openai and pluggy put the details."""
    exc = signal.exception_type
    if exc == "AttributeError" and (m := NUMPY_EXPIRED_RE.search(text)):
        signal.kind, signal.module, signal.name = "module_attribute", "numpy", m.group("name")
    elif exc == "AttributeError" and (m := NUMPY_MEMBER_REMOVED_RE.search(text)):
        signal.kind, signal.name = "object_attribute", m.group("name")
        signal.owner = NUMPY_MEMBER_OWNERS[m.group("owner")]
    elif exc == "AttributeError" and (m := LAZY_ATTR_RE.match(text.split("\n", 1)[0])):
        signal.kind, signal.module, signal.name = "module_attribute", m.group("module"), m.group("name")
    elif exc == "PydanticImportError" and (m := PYDANTIC_REMOVED_RE.search(text)):
        signal.kind, signal.module, signal.name = "import_name", m.group("module"), m.group("name")
    elif exc == "PydanticImportError" and (m := PYDANTIC_MOVED_RE.search(text)):
        signal.kind, signal.module, signal.name = "import_name", "pydantic", m.group("name")
    elif exc == "PydanticUserError" and (m := PYDANTIC_REMOVED_KWARG_RE.search(text)):
        # pydantic documents `removed-kwargs` as Field()'s error; conlist() raises it too, and its frame says so.
        raiser = signal.frames[-1] if signal.frames else None
        from_pydantic = raiser is not None and (raiser.module or "").split(".")[0] == "pydantic"
        signal.kind, signal.kwarg = "unexpected_kwarg", m.group("kwarg")
        signal.owner = raiser.function if from_pydantic else "Field"
    elif exc == "APIRemovedInV1" and (m := OPENAI_V1_RE.search(text)):
        signal.kind, signal.module, signal.name = "module_attribute", "openai", m.group("name")
    elif exc == "PluginValidationError" and (m := HOOK_ARG_RE.search(text)):
        # A plugin's hook implementation takes an argument the hookspec no longer provides.
        signal.kind, signal.owner, signal.kwarg = "unexpected_kwarg", m.group("hook"), m.group("arg")


def _classify(signal: ErrorSignal, msg: str) -> None:
    if m := CIRCULAR_RE.search(msg):
        signal.kind, signal.module = "circular_import", m.group("module")
    elif m := IMPORT_NAME_RE.search(msg):
        signal.kind, signal.module, signal.name = "import_name", m.group("module"), m.group("name")
    elif (m := BARE_IMPORT_NAME_RE.search(msg)) and (
        imported := [r for r in signal.referenced_names if r.endswith("." + m.group("name"))]
    ):
        # A pasted message trimmed to `cannot import name 'X'`: the frame's `from M import X` names the module.
        signal.kind, signal.name = "import_name", m.group("name")
        signal.module = imported[-1].removesuffix("." + m.group("name"))
    elif m := NO_MODULE_RE.search(msg):
        signal.kind, signal.module = "module_missing", m.group("module")
    elif m := MODULE_ATTR_RE.search(msg):
        signal.kind, signal.module, signal.name = "module_attribute", m.group("module"), m.group("name")
    elif m := TYPE_ATTR_RE.search(msg):
        signal.kind, signal.owner, signal.name = "class_attribute", m.group("owner"), m.group("name")
    elif m := OBJ_ATTR_RE.search(msg):
        signal.kind, signal.owner, signal.name = "object_attribute", m.group("owner"), m.group("name")
    elif m := UNEXPECTED_KW_RE.search(msg):
        signal.kind, signal.owner, signal.kwarg = "unexpected_kwarg", m.group("owner"), m.group("kwarg")
    elif m := BARE_KW_RE.search(msg):
        signal.kind, signal.kwarg = "unexpected_kwarg", m.group("kwarg")
    elif m := MISSING_ARG_RE.search(msg):
        signal.kind, signal.owner = "missing_argument", m.group("owner")
        signal.missing_args = re.findall(r"'(\w+)'", m.group("names"))
    elif m := TOO_MANY_POS_RE.search(msg):
        signal.kind, signal.owner = "too_many_positional", m.group("owner")
