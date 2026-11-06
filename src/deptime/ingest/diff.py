"""Change events between consecutive snapshots of one package.

Events are facts about *public paths* (what users import). Attribute value changes are ignored on purpose:
they are noise for "why does my code break" (griffe reports `__version__` changes as breakages, for example).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from deptime.ingest.replacements import Hint, param_alternatives, param_deprecations
from deptime.ingest.snapshot import HIDDEN, ApiSnapshot, SymbolRecord, resolve_member

MAX_MOVED_CANDIDATES = 3
REQUIRED_KINDS = {"positional-only", "positional or keyword", "keyword-only"}
NAMED_KINDS = {"positional or keyword", "keyword-only"}  # what a caller can pass by name


@dataclass
class ChangeEvent:
    path: str
    kind: str  # added | removed | moved | kind_changed | deprecated | param_deprecated | param_added | param_removed | param_kind_changed | param_now_required | param_now_optional
    detail: dict[str, Any] = field(default_factory=dict)


def _params(rec: SymbolRecord) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in (rec.params or [])}


def is_required(param: dict[str, Any]) -> bool:
    """A parameter callers must pass: no default, and not variadic."""
    return param.get("default") is None and param.get("kind") in REQUIRED_KINDS


def accepts_var_keyword(rec: SymbolRecord) -> bool:
    return any(p.get("kind") == "variadic keyword" for p in rec.params or [])


def _by_name(param: dict[str, Any]) -> bool:
    """A parameter callers pass by name: not variadic, not positional-only, and not named `__x`, PEP 484's
    convention for positional-only (pydantic's `__value`, starlette's `url_path_for(__name)`)."""
    name = param["name"]
    return param.get("kind") in NAMED_KINDS and not (name.startswith("__") and not name.endswith("__"))


def _plain(name: str) -> str:
    return name.lower().replace("_", "")


def renamed_params(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Removed parameters that come back in the same release in another case or with other underscores, old -> new:
    mcp 2.0's `requestedSchema` -> `requested_schema`, gradio 4.0's `_js` -> `js`, `id` -> `id_` (DT-CORE-24).

    Both must be passed by name, the new one must not be private (`obj` -> `_obj` makes it internal, not
    renamed), and the match must be one to one."""
    gone: dict[str, list[str]] = defaultdict(list)
    came: dict[str, list[str]] = defaultdict(list)
    for name in old.keys() - new.keys():
        if _by_name(old[name]):
            gone[_plain(name)].append(name)
    for name in new.keys() - old.keys():
        if _by_name(new[name]) and not name.startswith("_"):
            came[_plain(name)].append(name)
    return {
        names[0]: came[key][0]
        for key, names in gone.items()
        if len(names) == 1 and len(came.get(key, ())) == 1
    }


def _param_event(path: str, param: str, message: str | None, hints: list[Hint]) -> ChangeEvent:
    return ChangeEvent(
        path,
        "param_deprecated",
        {
            "param": param,
            "message": message,
            "alternatives": [{"target": h.target, "confidence": h.confidence} for h in hints],
        },
    )


def _deprecation_events(path: str, rec: SymbolRecord, old: SymbolRecord | None = None) -> list[ChangeEvent]:
    """What `rec` deprecates that `old` (the previous release's record, if any) didn't, or said differently.

    The whole symbol's text can still deprecate arguments by its wording (a decorator's "'x' is deprecated");
    `deprecated_params` are the arguments the function's own warnings deprecate (DT-DATA-42)."""
    events: list[ChangeEvent] = []
    names = list(_params(rec))
    if rec.deprecated and (old is None or rec.deprecated != old.deprecated):
        per_param = param_deprecations(rec.deprecated, names)
        if per_param:
            events.extend(_param_event(path, p, rec.deprecated, hints) for p, hints in per_param.items())
        else:
            events.append(ChangeEvent(path, "deprecated", {"message": rec.deprecated}))
    current = rec.deprecated_params or {}
    before = (old.deprecated_params or {}) if old is not None else {}
    done = {e.detail["param"] for e in events if e.kind == "param_deprecated"}
    for param, message in current.items():
        if param in done or before.get(param) == message:
            continue
        siblings = [p for p, m in current.items() if m == message]  # "'a' and 'b' are deprecated"
        events.append(_param_event(path, param, message, param_alternatives(message, siblings, names)))
    return events


def _module_level(snap: ApiSnapshot, path: str) -> bool:
    """A symbol defined directly in a module (not a class member)."""
    parent = snap.symbols.get(path.rsplit(".", 1)[0])
    return parent is not None and parent.kind == "module"


def initial_path_events(path: str, rec: SymbolRecord) -> list[ChangeEvent]:
    """The events of a path in the first indexed release: its deprecations, as `initial_events` reports them."""
    return _deprecation_events(path, rec)


def initial_events(snapshot: ApiSnapshot) -> list[ChangeEvent]:
    """Deprecations already present in the first indexed release (no previous release to diff against)."""
    events: list[ChangeEvent] = []
    for path in sorted(snapshot.symbols):
        events.extend(_deprecation_events(path, snapshot.symbols[path]))
    if snapshot.own_members:
        events.extend(member_events(None, snapshot))
    return events


def _changed(path: str, a: SymbolRecord, b: SymbolRecord) -> list[ChangeEvent]:
    """What changed on a path both releases have."""
    events: list[ChangeEvent] = []
    if a.kind != b.kind:
        events.append(ChangeEvent(path, "kind_changed", {"from": a.kind, "to": b.kind}))
    if a.params is not None and b.params is not None:
        pa, pb = _params(a), _params(b)
        renamed = renamed_params(pa, pb)
        for name in sorted(pa.keys() - pb.keys()):
            detail: dict[str, Any] = {"param": name, "absorbed_by_var_keyword": accepts_var_keyword(b)}
            if name in renamed:
                detail["renamed_to"] = renamed[name]
            events.append(ChangeEvent(path, "param_removed", detail))
        for name in sorted(pb.keys() - pa.keys()):
            events.append(
                ChangeEvent(path, "param_added", {"param": name, "required": is_required(pb[name])})
            )
        for name in sorted(pa.keys() & pb.keys()):
            if pa[name].get("kind") != pb[name].get("kind"):
                events.append(
                    ChangeEvent(
                        path,
                        "param_kind_changed",
                        {"param": name, "from": pa[name].get("kind"), "to": pb[name].get("kind")},
                    )
                )
            was, now = is_required(pa[name]), is_required(pb[name])
            if was != now:
                kind = "param_now_required" if now else "param_now_optional"
                events.append(
                    ChangeEvent(
                        path,
                        kind,
                        {
                            "param": name,
                            "default": pa[name].get("default") if now else pb[name].get("default"),
                        },
                    )
                )
    events.extend(_deprecation_events(path, b, a))
    return events


def path_events(path: str, old: SymbolRecord | None, new: SymbolRecord | None) -> list[ChangeEvent]:
    """The events between two consecutive releases' records of one path (None where a release doesn't have it),
    as `diff_snapshots` reports them, less `moved`, which pairs module-level paths with each other."""
    if old is None and new is None:
        return []
    if old is None:
        # A name can arrive deprecated: restored for a while (matplotlib 3.9.1's `cm.get_cmap`), or a new path to
        # a deprecated function (DT-CORE-23).
        return [ChangeEvent(path, "added", {"kind": new.kind}), *_deprecation_events(path, new)]  # type: ignore[union-attr]
    if new is None:
        return [ChangeEvent(path, "removed", {"kind": old.kind})]
    return _changed(path, old, new)


def diff_snapshots(old: ApiSnapshot, new: ApiSnapshot) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    old_paths, new_paths = set(old.symbols), set(new.symbols)
    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)

    # "Moved" = a module-level symbol disappeared and one with the same name and kind appeared at module level.
    # Class members are never paired: leaves like `__init__`, `close` or `validate` collide constantly, and when
    # a class moves, its own event already says so. More than MAX_MOVED_CANDIDATES matches is no evidence at all.
    added_by_leaf: dict[str, list[str]] = defaultdict(list)
    for path in added:
        events.extend(path_events(path, None, new.symbols[path]))
        if _module_level(new, path):
            added_by_leaf[path.rsplit(".", 1)[-1]].append(path)

    for path in removed:
        rec = old.symbols[path]
        events.extend(path_events(path, rec, None))
        if not _module_level(old, path):
            continue
        leaf = path.rsplit(".", 1)[-1]
        same_kind = [q for q in added_by_leaf.get(leaf, []) if new.symbols[q].kind == rec.kind]
        if len(same_kind) <= MAX_MOVED_CANDIDATES:
            events.extend(ChangeEvent(path, "moved", {"to": target}) for target in same_kind)

    for path in sorted(old_paths & new_paths):
        events.extend(_changed(path, old.symbols[path], new.symbols[path]))
    if old.own_members or new.own_members:
        events.extend(member_events(old, new))
    return events


def labels(snap: ApiSnapshot) -> dict[str, str]:
    """Where each class of a snapshot stored with own members has its members' events (DT-DATA-16): its shortest
    public path (fewest dots, then length), else its canonical path, for a base no public path names."""
    out: dict[str, str] = {}
    for path, rec in snap.symbols.items():
        if rec.kind == "class" and rec.canonical in snap.classes:
            best = out.get(rec.canonical)
            if best is None or (path.count("."), len(path), path) < (best.count("."), len(best), best):
                out[rec.canonical] = path
    return {cls: out.get(cls, cls) for cls in snap.classes}


def own_names(snap: ApiSnapshot, cls: str) -> list[str]:
    rec = snap.classes.get(cls)
    return [n for n, m in rec.members.items() if m.kind != HIDDEN] if rec is not None else []


def member_events(old: ApiSnapshot | None, new: ApiSnapshot) -> list[ChangeEvent]:
    """Events on class members for a package stored with own members (DT-DATA-16), each once, at the label of the
    class that defines it: not on every subclass that inherits it. For each class, the names it defines in either
    release are compared as the class resolves them, so a method that moves to a base class isn't removed. A class
    that moves keeps its label's history when the label stays. `old` None: the first release, deprecations only."""
    if old is None:
        events: list[ChangeEvent] = []
        for cls, label in labels(new).items():
            for name in own_names(new, cls):
                rec = resolve_member(new.classes, cls, name)
                if rec is not None:
                    events.extend(_deprecation_events(f"{label}.{name}", rec))
        return events
    before, after = labels(old), labels(new)
    by_label = {label: cls for cls, label in before.items()}
    pairs: list[tuple[str | None, str | None, str]] = []
    matched: set[str] = set()
    for cls, label in after.items():
        prev = cls if cls in before else by_label.get(label)
        if prev is not None and prev not in matched and (prev == cls or prev not in after):
            matched.add(prev)
            pairs.append((prev, cls, label))
        else:
            pairs.append((None, cls, label))
    pairs.extend((cls, None, label) for cls, label in before.items() if cls not in matched)
    events = []
    for prev, cls, label in sorted(pairs, key=lambda p: p[2]):
        names = dict.fromkeys(
            [*(own_names(old, prev) if prev else ()), *(own_names(new, cls) if cls else ())]
        )
        for name in sorted(names):
            a = resolve_member(old.classes, prev, name) if prev else None
            b = resolve_member(new.classes, cls, name) if cls else None
            events.extend(path_events(f"{label}.{name}", a, b))
    return events
