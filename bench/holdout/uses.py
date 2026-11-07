"""What a project's code uses from one package (DT-QA-07): read with `ast`, never run, and independent of DepTime.

The holdout asks DepTime about these uses, so they must not come from DepTime's own reader (`snippet.py`): a use that
reader misses would never be asked, and recall would look better than it is. Two kinds of use:

- `name`: a module or name the code imports or reads, `numpy.linalg.norm`, written as its import path.
- `call`: a call to one, with its shape: how many positional arguments and which keywords. Calls that splat
  (`*args`, `**kw`) have no shape and are left out; the name still counts.

Either can be `via: instance`: an attribute read off an object the code built by calling a package name,
`client = httpx.Client()` then `client.get(...)`, written `httpx.Client.get`. Whether the builder is a class is
decided by the probe, at run time.

    python3 -I bench/holdout/uses.py <source dir> <import root> [<import root> ...]
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    "site-packages",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    "vendor",
    "_vendor",
    "third_party",
}
MAX_FILE_BYTES = 1_000_000


@dataclass
class Use:
    kind: str  # name | call
    path: str
    npos: int | None = None  # call shape: positional arguments passed
    kwargs: tuple[str, ...] = ()  # call shape: keyword arguments passed
    via: str = "import"  # "instance": a member of an object the code built by calling `path`'s parent
    where: list[str] = field(default_factory=list)  # file:line, first few

    @property
    def key(self) -> tuple:
        return (self.kind, self.path, self.npos, self.kwargs, self.via)


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + ".") for r in roots)


class _Reader(ast.NodeVisitor):
    def __init__(self, roots: tuple[str, ...], file: str, out: dict[tuple, Use]) -> None:
        self.roots, self.file, self.out = roots, file, out
        self.bound: dict[str, str] = {}  # local name -> package path it names
        self.built: dict[str, str] = {}  # local name -> package path called to build it

    def _add(
        self, kind: str, path: str, node: ast.AST, npos: int | None = None, kwargs=(), via: str = "import"
    ) -> None:
        use = Use(kind, path, npos, tuple(sorted(kwargs)), via)
        use = self.out.setdefault(use.key, use)
        where = f"{self.file}:{getattr(node, 'lineno', 0)}"
        if len(use.where) < 3 and where not in use.where:
            use.where.append(where)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if not _within(a.name, self.roots):
                continue
            self._add("name", a.name, node)
            if a.asname:
                self.bound[a.asname] = a.name
            else:
                top = a.name.split(".")[0]
                # `import google.genai` binds `google`; only the package's own root is a path we can name.
                root = next(r for r in self.roots if a.name == r or a.name.startswith(r + "."))
                if top == root:
                    self.bound[top] = top
                else:
                    self.bound.setdefault(top, top)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        if not _within(node.module, self.roots):
            # `from google import genai`: the module is a root's parent and the name completes it.
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if _within(full, self.roots):
                    self._add("name", full, node)
                    self.bound[a.asname or a.name] = full
            return
        self._add("name", node.module, node)
        for a in node.names:
            if a.name == "*":
                continue
            path = f"{node.module}.{a.name}"
            self._add("name", path, node)
            self.bound[a.asname or a.name] = path

    def _path(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            path = self.bound.get(node.id)
            return path if path and _within(path, self.roots) else None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in self.bound:
                base = self.bound[node.value.id]
                full = f"{base}.{node.attr}"
                if _within(full, self.roots):
                    return full
                return None
            base = self._path(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def _built_by(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.built.get(node.id)
        if isinstance(node, ast.Call):
            return self._path(node.func)
        return None

    @staticmethod
    def _shape(call: ast.Call) -> tuple[int, list[str]] | None:
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return None
        return len(call.args), [k.arg for k in call.keywords]

    def _forget(self, target: ast.expr) -> None:
        for n in ast.walk(target):
            if isinstance(n, ast.Name):
                self.bound.pop(n.id, None)
                self.built.pop(n.id, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        built = self._path(node.value.func) if isinstance(node.value, ast.Call) else None
        alias = self._path(node.value) if isinstance(node.value, (ast.Name, ast.Attribute)) else None
        for target in node.targets:
            self._forget(target)
            if isinstance(target, ast.Name):
                if built:
                    self.built[target.id] = built
                elif alias:
                    self.bound[target.id] = alias

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        self._forget(node.target)
        if (
            isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Call)
            and (built := self._path(node.value.func))
        ):
            self.built[node.target.id] = built

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if (
                isinstance(item.optional_vars, ast.Name)
                and isinstance(item.context_expr, ast.Call)
                and (built := self._path(item.context_expr.func)) is not None
            ):
                self.built[item.optional_vars.id] = built
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_Call(self, node: ast.Call) -> None:
        shape = self._shape(node)
        path = self._path(node.func)
        if path is not None:
            self._add("name", path, node)
            if shape is not None:
                self._add("call", path, node, shape[0], shape[1])
        elif isinstance(node.func, ast.Attribute) and (built := self._built_by(node.func.value)):
            member = f"{built}.{node.func.attr}"
            self._add("name", member, node, via="instance")
            if shape is not None:
                self._add("call", member, node, shape[0], shape[1], via="instance")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            path = self._path(node)
            if path is not None:
                self._add("name", path, node)
                return
            if built := self._built_by(node.value):
                self._add("name", f"{built}.{node.attr}", node, via="instance")
                self.visit(node.value)
                return
        self.generic_visit(node)


def python_files(root: Path) -> list[Path]:
    found = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        found.append(p)
    return found


def uses_in_tree(root: Path, roots: tuple[str, ...]) -> list[Use]:
    out: dict[tuple, Use] = {}
    for p in python_files(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        _Reader(roots, str(p.relative_to(root)), out).visit(tree)
    return sorted(
        out.values(), key=lambda u: (u.path, u.kind, u.via, u.npos if u.npos is not None else -1, u.kwargs)
    )


def as_dict(u: Use) -> dict:
    d = {"kind": u.kind, "path": u.path}
    if u.kind == "call":
        d.update(npos=u.npos, kwargs=list(u.kwargs))
    if u.via != "import":
        d["via"] = u.via
    d["where"] = u.where
    return d


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    uses = uses_in_tree(Path(sys.argv[1]), tuple(sys.argv[2:]))
    for u in uses:
        print(json.dumps(as_dict(u)))


if __name__ == "__main__":
    main()
