"""griffe extraction against a tiny on-disk package (no network)."""

import gc
from pathlib import Path

import griffe

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
        "import markupsafe\n"
        "from typing import Any, TYPE_CHECKING\n"
        "from shutil import get_terminal_size as get_terminal_size\n"
        "from markupsafe import escape\n"
        "from click.termui import clear as clear\n"
        "from click import echo as say\n"
        "from typing_extensions import Self\n"
        "from flasky.app import Flask as Flask\n"
        "from flasky.app import helper, _hidden\n"
        "if TYPE_CHECKING:\n"
        "    from markupsafe import Markup\n"
        "try:\n"
        "    from orjson import dumps\n"
        "except ImportError:\n"
        "    pass\n"
    ),
    "flasky/app.py": (
        "from markupsafe import escape\n"
        "from flasky.util import tool\n\n\n"
        "class Flask:\n    pass\n\n\n"
        "def helper():\n    pass\n\n\n"
        "def _hidden():\n    pass\n"
    ),
    "flasky/util.py": "def tool():\n    pass\n",
    "flasky/ext/__init__.py": (
        "from pydantic import BaseModel\nfrom starlette import status as status\n\n\nclass Settings(BaseModel):\n    pass\n"
    ),
    "strict/__init__.py": 'from markupsafe import escape\nfrom strict.core import listed, unlisted\n__all__ = ["listed"]\n',
    "strict/core.py": "def listed():\n    pass\n\n\ndef unlisted():\n    pass\n",
}


# sqlalchemy's `schema.py` (1.4: plain imports only) and `types.py` (2.x: `as` re-exports) are plain modules.
FACADES = {
    "alchemy/__init__.py": "",
    "alchemy/sql/__init__.py": "",
    "alchemy/sql/schema.py": "class Column:\n    pass\n\n\nclass MetaData:\n    pass\n\n\ndef _key():\n    pass\n",
    "alchemy/schema.py": (
        '"""Compatibility namespace for alchemy.sql.schema."""\n\n'
        "from typing import Any\n"
        "from markupsafe import Markup\n"
        "from .sql.schema import Column  # noqa\n"
        "from .sql.schema import _key  # noqa\n"
    ),
    "alchemy/types.py": (
        "from .sql.schema import MetaData as MetaData\nfrom .sql.schema import Column\n\n\ndef helper():\n    pass\n"
    ),
    "alchemy/engine.py": "from alchemy.sql.schema import Column\n\n\ndef create():\n    pass\n",
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


# pytest's API lives in the private top-level package `_pytest`, shipped in the same wheel.
SIBLING = {
    "testy/__init__.py": (
        "from _testy.outcomes import skip\n"
        "from _testy.main import Session\n"
        "from other.thing import helper\n"
        '__all__ = ["skip", "Session", "helper"]\n'
    ),
    "_testy/__init__.py": "",
    "_testy/outcomes.py": "def skip(reason='', *, allow_module_level=False, msg=None):\n    pass\n",
    "_testy/main.py": "class Session:\n    def __init__(self, config):\n        pass\n",
    "other/__init__.py": "",
    "other/thing.py": "def helper(x):\n    pass\n",
}


# Module `__getattr__` shims, shaped like pydantic-ai 1.100 (rename map), werkzeug 3.0 (`__version__`),
# django 4.2 (`if name != "utc": raise`) and numpy 2.0 (raises for removed names: those must stay absent).
SHIMS = {
    "shimmy/__init__.py": (
        "import warnings\n"
        "from shimmy.core import NewPart\n\n"
        "_RENAMED: dict[str, str] = {'OldPart': 'NewPart'}\n"
        "__expired__ = {'float_': 'Use `np.float64` instead.'}\n\n\n"
        "def __getattr__(name):\n"
        "    if name in _RENAMED:\n"
        "        new_name = _RENAMED[name]\n"
        "        warnings.warn(f'`shimmy.{name}` is deprecated, use `shimmy.{new_name}` instead.', DeprecationWarning)\n"
        "        return globals()[new_name]\n"
        "    elif name == '__version__':\n"
        "        return '1.0'\n"
        "    elif name == 'NewPart' or name == 'lazy':\n"
        "        return NewPart\n"
        "    if name in __expired__:\n"
        "        raise AttributeError(__expired__[name])\n"
        "    if name in ('_private', 'gone'):\n"
        "        if name == 'gone':\n"
        "            raise AttributeError(name)\n"
        "        return 1\n"
        "    raise AttributeError(name)\n"
    ),
    "shimmy/core.py": "class NewPart:\n    pass\n",
    "shimmy/tz.py": (
        "import warnings\nfrom datetime import timezone\n\n\n"
        "def __getattr__(name):\n"
        "    if name != 'utc':\n"
        "        raise AttributeError(name)\n"
        "    warnings.warn(\n"
        "        'The shimmy.tz.utc alias is deprecated. '\n"
        "        'Please use datetime.timezone.utc instead.',\n"
        "        RemovedInShimmy2Warning,\n"
        "    )\n"
        "    return timezone.utc\n"
    ),
    # fastmcp 2.12's warning is conditional; numpy 1.26's `core.MachAr` says "private API" with a DeprecationWarning.
    "shimmy/legacy.py": (
        "import warnings\nSETTINGS = None\n\n\n"
        "def __getattr__(name):\n"
        "    if name == 'Image':\n"
        "        if SETTINGS.deprecation_warnings:\n"
        "            warnings.warn('`shimmy.legacy.Image` is considered private API', DeprecationWarning)\n"
        "        return 1\n"
        "    raise AttributeError(name)\n"
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


def test_package_init_reexports_from_other_distributions(tmp_path):
    # DT-DATA-21: typer re-exports click's API (`as clear`); flask 2.2 imports `escape` from markupsafe.
    snap = build_snapshot("flasky", "1.0", ["flasky", "strict"], _write(tmp_path, IMPORTS))
    found = {p: (r.kind, r.canonical, r.params) for p, r in snap.symbols.items()}
    assert found["flasky.escape"] == ("alias", "markupsafe.escape", None)
    assert found["flasky.clear"] == ("alias", "click.termui.clear", None)
    assert found["flasky.get_terminal_size"] == ("alias", "shutil.get_terminal_size", None)
    assert found["flasky.ext.status"] == ("alias", "starlette.status", None)  # fastapi.status
    # Out: module imports, plain stdlib imports, renames, conditional imports, plain modules, names `__all__`
    # omits, and plain imports in a subpackage (chromadb.server.fastapi imports pydantic's BaseModel to use it).
    assert not {
        "flasky.markupsafe",
        "flasky.TYPE_CHECKING",
        "flasky.Self",
        "flasky.say",
        "flasky.Markup",
        "flasky.dumps",
        "flasky.app.escape",
        "flasky.ext.BaseModel",
        "strict.escape",
    } & set(found)


def test_plain_modules_that_reexport(tmp_path):
    # DT-DATA-24: without this, sqlalchemy.types.Integer looked removed in 2.0.0.
    snap = build_snapshot("alchemy", "1.0", ["alchemy"], _write(tmp_path, FACADES))
    found = {p: (r.kind, r.canonical) for p, r in snap.symbols.items()}
    assert found["alchemy.schema.Column"] == ("class", "alchemy.sql.schema.Column")
    assert found["alchemy.types.MetaData"] == ("class", "alchemy.sql.schema.MetaData")
    assert found["alchemy.schema.Markup"] == (
        "alias",
        "markupsafe.Markup",
    )  # a module of imports re-exports them
    # A module with its own code imports to use: plain imports there stay private, as do stdlib and `_` names.
    assert not {
        "alchemy.types.Column",
        "alchemy.engine.Column",
        "alchemy.schema.Any",
        "alchemy.schema._key",
    } & set(found)


def test_stub_redefined_init_imports_are_public(tmp_path):
    snap = build_snapshot("stubby", "1.0", ["stubby"], _write(tmp_path, STUBS))
    paths = set(snap.symbols)
    assert {"stubby.add", "stubby.float64"} <= paths
    assert [p["name"] for p in snap.symbols["stubby.add"].params] == ["x", "y"]
    assert not {"stubby._private", "stubby.TypeAlias"} & paths


def test_private_sibling_package_is_resolved(tmp_path):
    snap = build_snapshot("testy", "1.0", ["testy"], _write(tmp_path, SIBLING))
    skip = snap.symbols["testy.skip"]
    assert (skip.kind, skip.canonical) == ("function", "_testy.outcomes.skip")
    assert [p["name"] for p in skip.params] == ["reason", "allow_module_level", "msg"]
    assert [p["name"] for p in snap.symbols["testy.Session.__init__"].params] == ["config"]
    # Only the `_testy` sibling is loaded; another distribution's names stay aliases (DT-DATA-21).
    assert snap.symbols["testy.helper"].kind == "alias"
    assert not any(p.startswith("_testy") for p in snap.symbols)


def test_names_a_module_getattr_serves_are_present(tmp_path):
    # DT-DATA-20: a removed name kept alive by a deprecation shim is still importable.
    snap = build_snapshot("shimmy", "1.0", ["shimmy"], _write(tmp_path, SHIMS))
    old = snap.symbols["shimmy.OldPart"]
    assert (old.kind, old.canonical) == ("getattr", "shimmy.__getattr__")
    assert old.deprecated == "`shimmy.OldPart` is deprecated, use `shimmy.NewPart` instead."
    assert (snap.symbols["shimmy.__version__"].kind, snap.symbols["shimmy.__version__"].deprecated) == (
        "getattr",
        None,
    )
    assert snap.symbols["shimmy.lazy"].kind == "getattr"
    assert snap.symbols["shimmy.NewPart"].kind == "class"  # a real member wins
    utc = snap.symbols["shimmy.tz.utc"]
    assert utc.kind == "getattr"
    assert (
        utc.deprecated == "The shimmy.tz.utc alias is deprecated. Please use datetime.timezone.utc instead."
    )
    assert snap.symbols["shimmy.legacy.Image"].deprecated == "`shimmy.legacy.Image` is considered private API"
    # Names whose branch raises (numpy 2.0's expired attributes) and private names stay absent.
    assert not {"shimmy.float_", "shimmy.gone", "shimmy._private"} & set(snap.symbols)


def test_a_build_leaves_no_griffe_objects_alive(tmp_path):
    # DT-DATA-18: griffe's dataclasses extension memoized every dataclass it saw, and through its parents the
    # whole loaded package, so a cold build grew by one package tree per release (litellm: 11.9 of 16 GB).
    files = {
        "dc/__init__.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Point:\n    x: int\n"
    }

    def live() -> int:
        gc.collect()
        return sum(isinstance(o, griffe.Object) for o in gc.get_objects())

    before = live()
    snap = build_snapshot("dc", "1.0", ["dc"], _write(tmp_path, files))
    assert [p["name"] for p in snap.symbols["dc.Point.__init__"].params] == ["x"]
    assert live() == before


def test_snapshot_roundtrip(tmp_path):
    snap = build_snapshot("fakepkg", "1.0.0", ["fakepkg"], _write(tmp_path / "src"))
    out = tmp_path / "s.json.gz"
    snap.save(out)
    again = ApiSnapshot.load(out)
    assert again.symbols == snap.symbols and again.version == "1.0.0"
