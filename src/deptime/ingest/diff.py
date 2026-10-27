"""Change events between consecutive snapshots of one package.

Events are facts about *public paths* (what users import). Attribute value changes are ignored on purpose:
they are noise for "why does my code break" (griffe reports `__version__` changes as breakages, for example).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from deptime.ingest.replacements import param_deprecations
from deptime.ingest.snapshot import ApiSnapshot, SymbolRecord

MAX_MOVED_CANDIDATES = 3
REQUIRED_KINDS = {"positional-only", "positional or keyword", "keyword-only"}


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


def _deprecation_events(path: str, rec: SymbolRecord) -> list[ChangeEvent]:
    per_param = param_deprecations(rec.deprecated, list(_params(rec)))
    if per_param:
        return [
            ChangeEvent(
                path,
                "param_deprecated",
                {
                    "param": p,
                    "message": rec.deprecated,
                    "alternatives": [{"target": h.target, "confidence": h.confidence} for h in hints],
                },
            )
            for p, hints in per_param.items()
        ]
    return [ChangeEvent(path, "deprecated", {"message": rec.deprecated})]


def _module_level(snap: ApiSnapshot, path: str) -> bool:
    """A symbol defined directly in a module (not a class member)."""
    parent = snap.symbols.get(path.rsplit(".", 1)[0])
    return parent is not None and parent.kind == "module"


def initial_events(snapshot: ApiSnapshot) -> list[ChangeEvent]:
    """Deprecations already present in the first indexed release (no previous release to diff against)."""
    events: list[ChangeEvent] = []
    for path in sorted(snapshot.symbols):
        rec = snapshot.symbols[path]
        if rec.deprecated:
            events.extend(_deprecation_events(path, rec))
    return events


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
        events.append(ChangeEvent(path, "added", {"kind": new.symbols[path].kind}))
        if _module_level(new, path):
            added_by_leaf[path.rsplit(".", 1)[-1]].append(path)

    for path in removed:
        rec = old.symbols[path]
        events.append(ChangeEvent(path, "removed", {"kind": rec.kind}))
        if not _module_level(old, path):
            continue
        leaf = path.rsplit(".", 1)[-1]
        same_kind = [q for q in added_by_leaf.get(leaf, []) if new.symbols[q].kind == rec.kind]
        if len(same_kind) <= MAX_MOVED_CANDIDATES:
            events.extend(ChangeEvent(path, "moved", {"to": target}) for target in same_kind)

    for path in sorted(old_paths & new_paths):
        a, b = old.symbols[path], new.symbols[path]
        if a.kind != b.kind:
            events.append(ChangeEvent(path, "kind_changed", {"from": a.kind, "to": b.kind}))
        if a.params is not None and b.params is not None:
            pa, pb = _params(a), _params(b)
            for name in sorted(pa.keys() - pb.keys()):
                events.append(
                    ChangeEvent(
                        path,
                        "param_removed",
                        {"param": name, "absorbed_by_var_keyword": accepts_var_keyword(b)},
                    )
                )
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
        if b.deprecated and b.deprecated != a.deprecated:
            events.extend(_deprecation_events(path, b))
    return events
