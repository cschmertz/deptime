"""griffe extraction against a tiny on-disk package (no network)."""

from pathlib import Path

from deptime.ingest.snapshot import ApiSnapshot, build_snapshot

FILES = {
    "fakepkg/__init__.py": 'from fakepkg._impl import Client, helper\n__all__ = ["Client", "helper", "sub"]\n',
    "fakepkg/_impl.py": (
        "import warnings\n\n"
        "class Client:\n"
        "    def __init__(self, *, proxy=None, proxies=None, timeout=5):\n"
        "        if proxies:\n"
        "            message = (\n"
        "                \"The 'proxies' argument is now deprecated.\"\n"
        "                \" Use 'proxy' or 'mounts' instead.\"\n"
        "            )\n"
        "            warnings.warn(message, DeprecationWarning)\n\n"
        "    def get(self, url):\n"
        "        return url\n\n"
        "    def _private(self):\n"
        "        pass\n\n"
        "def helper(x, y=1):\n"
        "    return x\n"
    ),
    "fakepkg/sub/__init__.py": "def thing():\n    pass\n",
}


# `from .main import *` re-exports everything `main` imports, including the package itself (litellm, sqlalchemy).
REEXPORTS = {
    "loopy/__init__.py": "from loopy.main import *\n",
    "loopy/main.py": "import loopy\nfrom loopy.lib import scimath as emath\n\n\ndef run():\n    pass\n",
    "loopy/plain.py": "from loopy.main import *\n",
    "loopy/lib/__init__.py": "",
    "loopy/lib/scimath.py": "def sqrt(x):\n    return x\n",
    "loopy/lib/test_scimath.py": "def test_sqrt():\n    pass\n",
    "loopy/tests/__init__.py": "",
    "loopy/tests/test_main.py": "def test_run():\n    pass\n",
    "loopy/conftest.py": "def fixture():\n    pass\n",
}


# Without `__all__`, a package's `__init__` exposes its API by importing it (flask, fastapi, click, sqlalchemy).
IMPORTS = {
    "flasky/__init__.py": (
        "import os\n"
        "from typing import Any\n"
        "from flasky.app import Flask as Flask\n"
        "from flasky.app import helper, _hidden\n"
    ),
    "flasky/app.py": (
        "from flasky.util import tool\n\n\n"
        "class Flask:\n    pass\n\n\n"
        "def helper():\n    pass\n\n\n"
        "def _hidden():\n    pass\n"
    ),
    "flasky/util.py": "def tool():\n    pass\n",
    "strict/__init__.py": 'from strict.core import listed, unlisted\n__all__ = ["listed"]\n',
    "strict/core.py": "def listed():\n    pass\n\n\ndef unlisted():\n    pass\n",
}


# numpy 2.0/2.1: `__all__` is built at runtime, and `__init__.py` imports names defined in compiled code, so griffe
# replaces those imports with their definitions in `__init__.pyi`.
STUBS = {
    "stubby/__init__.py": (
        "from stubby import _core\n"
        "from stubby._core import add, float64, _private\n"
        "__all__ = list(set(_core.__all__))\n"
    ),
    "stubby/__init__.pyi": (
        "from typing import TypeAlias\n\n"
        "__all__: list[str]\n\n"
        "float64: TypeAlias = float\n\n\n"
        "def add(x: int, y: int) -> int: ...\n"
    ),
    "stubby/_core.py": (
        '__all__ = ["add", "float64"]\nfrom stubby._umath import *  # compiled: no source to read\n\n\n'
        "def _private():\n    pass\n"
    ),
}


def _write(root: Path, files: dict[str, str] = FILES) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_public_paths_signatures_and_deprecations(tmp_path):
    snap = build_snapshot("fakepkg", "1.0.0", ["fakepkg"], _write(tmp_path))
    paths = set(snap.symbols)
    assert {
        "fakepkg",
        "fakepkg.Client",
        "fakepkg.Client.__init__",
        "fakepkg.Client.get",
        "fakepkg.helper",
        "fakepkg.sub",
        "fakepkg.sub.thing",
    } <= paths
    assert "fakepkg.Client._private" not in paths
    assert not any("._impl" in p for p in paths)
    init = snap.symbols["fakepkg.Client.__init__"]
    assert [p["name"] for p in init.params] == ["proxy", "proxies", "timeout"]
    assert init.canonical == "fakepkg._impl.Client.__init__"
    assert "'proxies' argument is now deprecated" in init.deprecated


def test_reexported_modules_are_walked_once_and_test_suites_skipped(tmp_path):
    snap = build_snapshot("loopy", "1.0", ["loopy"], _write(tmp_path, REEXPORTS))
    assert set(snap.symbols) == {
        "loopy",
        "loopy.run",
        "loopy.main",
        "loopy.main.run",
        "loopy.lib",
        "loopy.lib.scimath",
        "loopy.lib.scimath.sqrt",
        # A package `__init__` re-exporting a module under a new name is walked there once (`numpy.emath`).
        "loopy.emath",
        "loopy.emath.sqrt",
        # Re-exports of the package itself, or from a plain module, are recorded but not walked.
        "loopy.loopy",
        "loopy.plain",
        "loopy.plain.run",
        "loopy.plain.emath",
        "loopy.plain.loopy",
    }
    assert snap.symbols["loopy.loopy"].kind == "module" and snap.symbols["loopy.loopy"].canonical == "loopy"


def test_package_init_imports_are_public_without_all(tmp_path):
    snap = build_snapshot("flasky", "1.0", ["flasky", "strict"], _write(tmp_path, IMPORTS))
    paths = set(snap.symbols)
    assert {"flasky.Flask", "flasky.helper", "strict.listed"} <= paths
    assert snap.symbols["flasky.Flask"].canonical == "flasky.app.Flask"
    # Private names, other packages' objects, imports in plain modules, and anything `__all__` leaves out stay out.
    assert not {"flasky._hidden", "flasky.os", "flasky.Any", "flasky.app.tool", "strict.unlisted"} & paths


def test_stub_redefined_init_imports_are_public(tmp_path):
    snap = build_snapshot("stubby", "1.0", ["stubby"], _write(tmp_path, STUBS))
    paths = set(snap.symbols)
    assert {"stubby.add", "stubby.float64"} <= paths
    assert [p["name"] for p in snap.symbols["stubby.add"].params] == ["x", "y"]
    assert not {"stubby._private", "stubby.TypeAlias"} & paths


def test_snapshot_roundtrip(tmp_path):
    snap = build_snapshot("fakepkg", "1.0.0", ["fakepkg"], _write(tmp_path / "src"))
    out = tmp_path / "s.json.gz"
    snap.save(out)
    again = ApiSnapshot.load(out)
    assert again.symbols == snap.symbols and again.version == "1.0.0"
