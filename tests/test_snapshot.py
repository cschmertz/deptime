"""griffe extraction against a tiny on-disk package (no network)."""

import gc
import textwrap
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


# numpy 1.25's chain to `numpy.product`: stubs that only annotate `__all__`, and `.py` files that build it with
# `.extend()`, `+=` and a helper that appends another module's `__all__`; griffe reads none of these.
RUNTIME_ALL = {
    "rtall/__init__.py": (
        "from rtall import core\n"
        "from rtall.core import *\n"
        "from rtall import lib\n"
        "from rtall.lib import *\n\n"
        "__all__ = ['core', 'lib']\n"
        "__all__.extend(core.__all__)\n"
        "__all__.extend(lib.__all__)\n"
    ),
    "rtall/__init__.pyi": (
        "from rtall.core.fromnumeric import sum as sum\n\n__all__: list[str]\n\nArrayLike = object\n"
    ),
    "rtall/core/__init__.py": (
        "from rtall.core import numeric\nfrom rtall.core.numeric import *\n\n__all__ = []\n__all__ += numeric.__all__\n"
    ),
    "rtall/core/numeric.py": (
        "from rtall.core import fromnumeric\n"
        "from rtall.core.fromnumeric import *\n"
        "from rtall.core._compiled import *  # no source to read\n\n"
        "__all__ = ['ones', 'dragon']\n\n\n"
        "def extend_all(module):\n"
        "    existing = set(__all__)\n"
        "    for a in getattr(module, '__all__'):\n"
        "        if a not in existing:\n"
        "            __all__.append(a)\n\n\n"
        "def ones(shape):\n    pass\n\n\n"
        "extend_all(fromnumeric)\n"
    ),
    "rtall/core/fromnumeric.py": (
        "__all__ = ['sum', 'product']\n\n\ndef sum(a):\n    pass\n\n\ndef product(a):\n    pass\n"
    ),
    "rtall/core/numeric.pyi": "__all__: list[str]\n\n\ndef ones(shape: tuple[int, ...]) -> object: ...\n",
    "rtall/core/fromnumeric.pyi": "__all__: list[str]\n\n\ndef sum(a: object) -> object: ...\n",
    "rtall/lib/__init__.py": (
        "import math\n\n__all__ = ['math', 'tool']\n\n\ndef tool():\n    pass\n\n\nclass NumpyVersion:\n    pass\n"
    ),
    "rtall/lib/__init__.pyi": "__all__: list[str]\n\n\ndef tool() -> None: ...\n\n\nclass NumpyVersion: ...\n",
}


# numpy 2's `ndarray`: `__init__.py` imports it from compiled code, and `__init__.pyi` defines most of its methods
# (`reshape`, `astype`, `item`) and some module functions only through `@overload`, with no implementation.
OVERLOADS = {
    "overly/__init__.py": "from overly._core import ndarray, where\nfrom overly.impl import pick\n",
    "overly/_core.py": "from overly._umath import *  # compiled: no source to read\n",
    "overly/__init__.pyi": (
        "from typing import Any, overload\n"
        "from typing_extensions import deprecated\n\n"
        "class _Common:\n"
        "    @overload\n"
        "    def astype(self, dtype: int, order: str = ..., *, copy: bool = ...) -> int: ...\n"
        "    @overload\n"
        "    def astype(self, dtype: str, order: str = ..., casting: str = ...) -> str: ...\n\n"
        "class ndarray(_Common):\n"
        "    def fill(self, value: Any) -> None: ...\n"
        "    @overload\n"
        "    def reshape(self, shape: int, /, *, order: str = ...) -> ndarray: ...\n"
        "    @overload\n"
        "    def reshape(self, *shape: int, order: str = ...) -> ndarray: ...\n"
        "    @overload\n"
        '    @deprecated("`ndarray.itemset` is deprecated, use `a[index] = value`")\n'
        "    def itemset(self, value: Any, /) -> None: ...\n"
        "    @overload\n"
        '    @deprecated("`ndarray.itemset` is deprecated, use `a[index] = value`")\n'
        "    def itemset(self, index: int, value: Any, /) -> None: ...\n"
        "    @overload\n"
        "    def item(self) -> Any: ...\n"
        "    @overload\n"
        '    @deprecated("passing `args` is deprecated")\n'
        "    def item(self, *args: int) -> Any: ...\n\n"
        "@overload\n"
        "def where(condition: Any, /) -> tuple[Any, ...]: ...\n"
        "@overload\n"
        "def where(condition: Any, x: Any, y: Any, /) -> Any: ...\n"
    ),
    "overly/impl.py": (
        "from typing import overload\n\n\n"
        "@overload\n"
        "def pick(x: int) -> int: ...\n"
        "@overload\n"
        "def pick(x: str, strict: bool) -> str: ...\n"
        "def pick(x, strict=False, *, fallback=None):\n"
        "    return x\n"
    ),
}


# pydantic 1.10's `types.py`: type checkers see `StrictBool = bool`; at runtime the `else:` branch defines the class,
# and the package re-exports it with `from .types import *` (pydantic 2's `NaiveDatetime` has the same shape).
GUARDED = {
    "guarded/__init__.py": "from guarded.types import *\n\n__all__ = ['StrictBool', 'ConstrainedInt', 'Checker']\n",
    "guarded/types.py": (
        "from typing import TYPE_CHECKING\n\n"
        "__all__ = ['StrictBool', 'ConstrainedInt', 'Checker']\n\n"
        "if TYPE_CHECKING:\n"
        "    StrictBool = bool\n"
        "    from collections.abc import Callable as Checker\n"
        "else:\n\n"
        "    class StrictBool(int):\n"
        "        pass\n\n\n"
        "class ConstrainedInt(int):\n"
        "    pass\n"
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
    # langchain 0.3.4's `chains/__init__.py`: lazy exports listed only as a dict's keys, served by one `return`.
    "shimmy/chains/__init__.py": (
        "from typing import Any\n\n"
        "from shimmy._api import create_importer\n\n"
        "_module_lookup = {\n"
        '    "NatBotChain": "shimmy.chains.natbot.base",\n'
        '    "APIChain": "shimmy.chains.api.base",\n'
        "}\n\n"
        "importer = create_importer(__package__, module_lookup=_module_lookup)\n\n\n"
        "def __getattr__(name: str) -> Any:\n"
        "    return importer(name)\n\n\n"
        "__all__ = list(_module_lookup.keys())\n"
    ),
    "shimmy/_api.py": "def create_importer(package, *, module_lookup=None):\n    pass\n",
    "shimmy/proxy.py": "def __getattr__(name):\n    return name\n",
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


# numpy 2.0 renamed `numpy.core` to `numpy._core` and left forwarders behind: the package and each submodule
# forward every attribute to their new home and warn through a helper in `numpy/core/_utils.py`.
FORWARDS = {
    "fwd/__init__.py": "",
    "fwd/_core/__init__.py": (
        "from fwd._core import numeric\n"
        "from fwd._core.numeric import *\n\n"
        "__all__ = ['numeric']\n"
        "__all__ += numeric.__all__\n"
    ),
    "fwd/_core/numeric.py": (
        "__all__ = ['array_equiv', 'ndarray', 'scalar']\n\n\n"
        "def array_equiv(a1, a2):\n    pass\n\n\n"
        "class ndarray:\n    def reshape(self, shape):\n        pass\n\n\n"
        "scalar = 1\n\n\n"
        "def _private():\n    pass\n"
    ),
    "fwd/core/__init__.py": (
        "from fwd import _core\n"
        "from ._utils import _raise_warning\n\n"
        "__all__ = ['numeric', 'umath']\n\n\n"
        "def __getattr__(attr_name):\n"
        "    attr = getattr(_core, attr_name)\n"
        "    _raise_warning(attr_name)\n"
        "    return attr\n"
    ),
    "fwd/core/_utils.py": (
        "import warnings\n\n\n"
        "def _raise_warning(attr: str, submodule: str | None = None) -> None:\n"
        "    new_module = 'fwd._core'\n"
        "    old_module = 'fwd.core'\n"
        "    if submodule is not None:\n"
        "        new_module = f'{new_module}.{submodule}'\n"
        "        old_module = f'{old_module}.{submodule}'\n"
        "    warnings.warn(\n"
        "        f'{old_module} is deprecated and has been renamed to {new_module}. '\n"
        "        f'use {new_module}.{attr}.',\n"
        "        DeprecationWarning,\n"
        "        stacklevel=3,\n"
        "    )\n"
    ),
    "fwd/core/numeric.py": (
        "from fwd._core import numeric\n\n"
        "# copied without a warning, for old pickles (numpy.core.multiarray's `scalar`)\n"
        "for item in ['scalar']:\n"
        "    globals()[item] = getattr(numeric, item)\n\n\n"
        "def __getattr__(attr_name):\n"
        "    from fwd._core import numeric\n"
        "    from ._utils import _raise_warning\n\n"
        "    sentinel = object()\n"
        "    ret = getattr(numeric, attr_name, sentinel)\n"
        "    if ret is sentinel:\n"
        "        raise AttributeError(attr_name)\n"
        "    _raise_warning(attr_name, 'numeric')\n"
        "    return ret\n"
    ),
    # A compiled target (numpy.core._multiarray_umath) has no source to read.
    "fwd/core/umath.py": (
        "def __getattr__(attr_name):\n    from fwd._core import _umath\n\n    return getattr(_umath, attr_name)\n"
    ),
}


# numpy 1.24-1.26 builds its table of deprecated aliases at import time, inside a module-level `if`/`else`, and
# reads it in a `try`; django 6.1's `core.mail` reads a literal table the same way; pydantic-ai's lazy exports
# fall through the `try` to a `return` after it.
TABLES = {
    "tabled/__init__.py": (
        "if __TABLED_SETUP__:\n"
        "    pass\n"
        "else:\n"
        "    __deprecated_attrs__ = {}\n"
        "    _financial_names = ['fv', 'pmt']\n"
        "    __expired_functions__ = {\n"
        "        name: (f'In accordance with NEP 32, the function {name} was removed '\n"
        "               'from NumPy version 1.20.')\n"
        "        for name in _financial_names}\n"
        "    _msg = \"module 'numpy' has no attribute '{n}'. `np.{n}` was a deprecated alias. {extended_msg}\"\n"
        "    _type_info = [('object', ''), ('bool', 'Use `np.bool_`.')]\n"
        "    __former_attrs__ = {\n"
        "        n: _msg.format(n=n, extended_msg=extended_msg) for n, extended_msg in _type_info}\n"
        "    _msg = '`np.{n}` is a deprecated alias for `{an}`.  (Deprecated NumPy 1.24)'\n"
        "    _type_info = [('bool8', bool, 'np.bool_'), ('int0', int, 'np.intp'), ('str0', str, '{0}')]\n"
        "    __deprecated_attrs__.update({\n"
        "        n: (alias, _msg.format(n=n, an=an)) for n, alias, an in _type_info})\n\n"
        "    import math\n\n"
        "    __deprecated_attrs__['math'] = (math,\n"
        "        '`np.math` is a deprecated alias for the standard library `math` '\n"
        "        'module (Deprecated Numpy 1.25). Replace usages of `np.math` with `math`')\n\n"
        "    del math, _msg, _type_info\n\n"
        "    def __getattr__(attr):\n"
        "        import warnings\n"
        "        try:\n"
        "            msg = __expired_functions__[attr]\n"
        "        except KeyError:\n"
        "            pass\n"
        "        else:\n"
        "            warnings.warn(msg, DeprecationWarning, stacklevel=2)\n\n"
        "            def _expired(*args, **kwds):\n"
        "                raise RuntimeError(msg)\n\n"
        "            return _expired\n\n"
        "        try:\n"
        "            val, msg = __deprecated_attrs__[attr]\n"
        "        except KeyError:\n"
        "            pass\n"
        "        else:\n"
        "            warnings.warn(msg, DeprecationWarning, stacklevel=2)\n"
        "            return val\n\n"
        "        if attr in __former_attrs__:\n"
        "            raise AttributeError(__former_attrs__[attr])\n"
        "        raise AttributeError(attr)\n"
    ),
    "tabled/mail/__init__.py": (
        "import warnings\n\n"
        "from tabled.mail.message import EmailMessage\n\n"
        "__all__ = ['EmailMessage', 'BadHeaderError', 'SafeMIMEText']\n\n"
        "_deprecate_on_import = {\n"
        "    'BadHeaderError': 'BadHeaderError is deprecated. Replace with ValueError.',\n"
        "    'SafeMIMEText': 'SafeMIMEText is deprecated.',\n"
        "}\n\n\n"
        "def __getattr__(name):\n"
        "    try:\n"
        "        msg = _deprecate_on_import[name]\n"
        "    except KeyError:\n"
        "        raise AttributeError(name) from None\n"
        "    else:\n"
        "        from tabled.mail import message\n\n"
        "        warnings.warn(msg, category=RemovedInTabled2Warning)\n"
        "        return getattr(message, name)\n"
    ),
    "tabled/mail/message.py": (
        "class EmailMessage:\n    pass\n\n\nclass BadHeaderError(ValueError):\n    pass\n\n\n"
        "class SafeMIMEText:\n    pass\n"
    ),
    "tabled/lazy.py": (
        "_exports = {'Thing': ('._impl', 'Thing')}\n\n\n"
        "def __getattr__(name):\n"
        "    from importlib import import_module\n\n"
        "    try:\n"
        "        module_name, attribute = _exports[name]\n"
        "    except KeyError:\n"
        "        raise AttributeError(name) from None\n"
        "    value = getattr(import_module(module_name, __name__), attribute)\n"
        "    globals()[name] = value\n"
        "    return value\n"
    ),
}


# pydantic 1.10.17's `pydantic/v1/__init__.py` is one line, `from pydantic import *`, and `pydantic` star-imports
# most of its own API from its submodules.
PARENT_STAR = {
    "pstar/__init__.py": (
        "from pstar.config import BaseConfig\n"
        "from pstar.main import *\n\n"
        "__all__ = ['BaseConfig', 'BaseModel', 'create_model']\n"
    ),
    "pstar/config.py": "class BaseConfig:\n    pass\n",
    "pstar/main.py": (
        "__all__ = ['BaseModel', 'create_model']\n\n\n"
        "class BaseModel:\n    def dict(self, exclude=None):\n        pass\n\n\n"
        "def create_model(name, **fields):\n    pass\n\n\n"
        "def helper():\n    pass\n"
    ),
    "pstar/v1/__init__.py": "from pstar import *\n",
    "pstar/v1/main.py": "from pstar.main import *\n",
}

# mcp 2.x keeps `mcp/server/fastmcp.py` only to raise `ModuleNotFoundError` with a migration hint, and dspy 3.4's
# `dspy/core/types.py` raises `ImportError` the same way. Raises under `if` or `try` (optional extras) don't count.
RAISES = {
    "rpkg/__init__.py": "from rpkg.server import Server\n",
    "rpkg/server/__init__.py": "class Server:\n    def run(self):\n        pass\n",
    "rpkg/server/legacy.py": (
        '"""Removed in rpkg 2: `Legacy` is now `rpkg.server.Server`."""\n\n'
        '_MESSAGE = (\n    "No module named \'rpkg.server.legacy\'. "\n    "Legacy was renamed to Server."\n)\n\n'
        "raise ModuleNotFoundError(_MESSAGE, name=__name__)\n"
    ),
    "rpkg/types/__init__.py": 'raise ImportError("rpkg 1.3 types were removed; use rpkg.server")\n',
    "rpkg/types/parts.py": "class Part:\n    pass\n",
    "rpkg/optional.py": (
        "try:\n    import numpy\nexcept ImportError:\n"
        '    raise ImportError("install rpkg[numpy]")\n\n\ndef fast():\n    pass\n'
    ),
    "rpkg/gated.py": (
        "import sys\n\nif sys.version_info < (3, 9):\n"
        '    raise RuntimeError("rpkg needs Python 3.9")\n\n\ndef run():\n    raise ValueError("no")\n'
    ),
}


# Folders without `__init__.py` are namespace packages to Python; griffe refuses to load them (DT-DATA-23).
NAMESPACES = {
    "nspkg/__init__.py": "from nspkg.types.utils import *\nfrom nspkg.llms.base import Model\n",
    "nspkg/types/utils.py": "class ModelResponse:\n    pass\n",
    "nspkg/llms/base.py": "class Model:\n    def run(self, prompt):\n        pass\n",
    "nspkg/llms/openai/chat.py": "def complete():\n    pass\n",
    "nspkg/contrib/real/__init__.py": "def tool():\n    pass\n",  # a folder with no files above a package
    "nspkg/proxy/__init__.py": "def serve():\n    pass\n",
    "nspkg/proxy/routes/admin.py": "def route():\n    pass\n",  # below a module coverage.toml skips
    "nspkg/tests/test_x.py": "def test_x():\n    pass\n",
    "nspkg/bad-name/mod.py": "def f():\n    pass\n",
    "nspkg/shadow.py": "def g():\n    pass\n",
    "nspkg/shadow/inner.py": "def h():\n    pass\n",  # Python imports the module, not the folder
    "nspkg/static/app.js": "x = 1\n",  # no Python below: not a package
}

# django and marshmallow warn with their own categories, `RemovedInDjango40Warning` (DT-DATA-36).
REMOVED_IN = {
    "djangoish/__init__.py": (
        "import warnings\n"
        "from djangoish.deprecation import RemovedInDjangoish40Warning\n\n\n"
        "def force_str(s):\n    return s\n\n\n"
        "def force_text(s):\n"
        "    warnings.warn(\n"
        "        'force_text() is deprecated in favor of force_str().',\n"
        "        RemovedInDjangoish40Warning,\n"
        "        stacklevel=2,\n"
        "    )\n"
        "    return force_str(s)\n"
    ),
    "djangoish/deprecation.py": "class RemovedInDjangoish40Warning(PendingDeprecationWarning):\n    pass\n",
}


# DT-DATA-42: the shapes of S2's misses. Only the first deprecation-like string in a function was read, and the
# function then read deprecated as a whole: `Field` "`const` is removed, use `Literal` instead" (from a raise).
WARNINGS = {
    "warny/__init__.py": textwrap.dedent(
        '''\
        import warnings

        REUSE_MSG = "`allow_reuse` is deprecated and does nothing."
        MISSING = object()
        NOT_PROVIDED = object()


        class WarnyDeprecatedSince20(DeprecationWarning):
            pass


        def validator(field, *, pre=False, allow_reuse=False):
            """Decorate a validator."""
            if allow_reuse is True:
                warnings.warn(REUSE_MSG, DeprecationWarning)
            warnings.warn(
                "V1 style `@validator` validators are deprecated."
                " You should migrate to V2 style `@field_validator` validators.",
                WarnyDeprecatedSince20,
                stacklevel=2,
            )


        def Field(default=None, *, min_length=None, json_schema_extra=None, **extra):
            if extra.get("const") is not None:
                raise TypeError("`const` is removed, use `Literal` instead")
            min_items = extra.pop("min_items", None)
            if min_items is not None:
                warnings.warn(
                    "`min_items` is deprecated and will be removed, use `min_length` instead",
                    DeprecationWarning,
                )
            if extra:
                warnings.warn(
                    "Using extra keyword arguments on `Field` is deprecated and will be removed."
                    " Use `json_schema_extra` instead.",
                    DeprecationWarning,
                )


        class StateGraph:
            def __init__(self, state_schema=None, context_schema=None, **kwargs):
                if (config_schema := kwargs.get("config_schema", MISSING)) is not MISSING:
                    warnings.warn(
                        "`config_schema` is deprecated and will be removed. Please use `context_schema` instead.",
                        category=WarnyDeprecatedSince20,
                    )
                if state_schema is None:
                    warnings.warn("Initializing StateGraph without state_schema is deprecated.", DeprecationWarning)


        class Agent:
            def __init__(self, model=None, *, output_type=str, **_deprecated_kwargs):
                if "result_type" in _deprecated_kwargs:
                    warnings.warn("`result_type` is deprecated, use `output_type` instead", DeprecationWarning)


        class Interface:
            def __init__(self, fn, flagging_mode=None, allow_flagging=None, cache_examples=False):
                if cache_examples:
                    warnings.warn("Cache examples cannot be used with state. Setting cache_examples to False.")
                if allow_flagging is not None:
                    warnings.warn(
                        "The `allow_flagging` parameter in `Interface` is deprecated."
                        "Use `flagging_mode` instead."
                    )


        class CheckConstraint:
            def __init__(self, *args, name, condition=None, check=None):
                if args:
                    warnings.warn("Passing positional arguments is deprecated.", DeprecationWarning)
                if check is not None:
                    warnings.warn(
                        "CheckConstraint.check is deprecated in favor of `.condition`.", DeprecationWarning
                    )


        def old_api():
            warnings.warn(DeprecationWarning("old_api() is deprecated, use new_api() instead"))


        def unreadable(x):
            warnings.warn(_message(), DeprecationWarning)


        def unreadable_param(x=None):
            if x is not None:
                warnings.warn(_message(x), DeprecationWarning)


        def deprecate_kwarg(old, new):
            def wrapper(func):
                def inner(*args, **kwargs):
                    if old in kwargs:
                        warnings.warn(f"{old} is deprecated, use {new}", DeprecationWarning)
                    return func(*args, **kwargs)

                return inner

            return wrapper


        def quiet():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                return 1


        def create_ssl_context(verify=True, cert=None):
            if verify is True:
                pass
            elif isinstance(verify, str):
                message = "`verify=<str>` is deprecated. Use `verify=ssl.create_default_context(cafile=...)`."
                warnings.warn(message, DeprecationWarning)
            if cert:
                message = "`cert=...` is deprecated. Use `verify=<ssl_context>` instead."
                warnings.warn(message, DeprecationWarning)


        class BarPlot:
            def __init__(self, value=None, key=None, **kwargs):
                for key, val in kwargs.items():
                    if key in ["x_label_angle", "show_actions_button"]:
                        warnings.warn(f"Argument '{key}' has been deprecated.", DeprecationWarning)


        def d2_log_loss_score(y_true, y_proba=None, y_pred="deprecated", sample_weight=None):
            if y_pred != "deprecated":
                warnings.warn(
                    "`y_pred` was renamed to `y_proba` in version 1.9. Use `y_proba` instead.", FutureWarning
                )


        def endpoint(values):
            if values["endpoint_type"] != "search":
                warnings.warn(f"`endpoint_type={values['endpoint_type']}` is deprecated.", DeprecationWarning)


        class ExclusionConstraint:
            def __init__(self, *, name, expressions, opclasses=()):
                if opclasses and len(expressions) != len(opclasses):
                    warnings.warn(
                        "The opclasses argument is deprecated in favor of using OpClass in"
                        " ExclusionConstraint.expressions.",
                        DeprecationWarning,
                    )


        _Default = object()


        class Retry:
            def __init__(self, total=10, allowed_methods=_Default, method_whitelist=_Default):
                if method_whitelist is not _Default:
                    warnings.warn(
                        "Using 'method_whitelist' with Retry is deprecated. Use 'allowed_methods' instead",
                        DeprecationWarning,
                    )


        class Store:
            def __init__(self, **kwargs):
                self._configuration = kwargs
                if self._configuration.get("textfield"):
                    warnings.warn("Store is deprecated, use NewStore instead.", DeprecationWarning)


        class Encoder:
            def __init__(self, sparse="deprecated", sparse_output=True):
                self.sparse = sparse

            def fit(self, X):
                if self.sparse != "deprecated":
                    warnings.warn("`sparse` was renamed to `sparse_output` in version 1.2.", FutureWarning)
                return self


        class Legacy:
            def __init__(self):
                warnings.warn("`Legacy` is deprecated, use `Modern` instead.", DeprecationWarning)


        def make_aware(value, timezone=None, is_dst=NOT_PROVIDED):
            if is_dst is NOT_PROVIDED:
                is_dst = None
            else:
                warnings.warn("The is_dst argument to make_aware() is deprecated.", DeprecationWarning)


        def enable_chunked_encoding(chunk_size=None):
            if chunk_size is not None:
                warnings.warn("Chunk size is deprecated #1615", DeprecationWarning)


        def get_random_string(length=NOT_PROVIDED):
            if length is NOT_PROVIDED:
                warnings.warn("Not providing a length argument is deprecated.", DeprecationWarning)


        def parse_all_fields(year_col, month_col):
            warnings.warn(
                """
                Use pd.to_datetime({"year": year_col, "month": month_col}) instead.
        """,
                FutureWarning,
            )


        def _message(x=None):
            return "deprecated"
        '''
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
    assert init.deprecated is None  # only the argument is (DT-DATA-42)
    assert "'proxies' argument is now deprecated" in init.deprecated_params["proxies"]


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


def test_names_a_runtime_all_lists_are_public(tmp_path):
    # DT-DATA-25: `numpy.product` looked removed in 1.25.0, when numpy's stub dropped it, instead of 2.0.0.
    snap = build_snapshot("rtall", "1.0", ["rtall"], _write(tmp_path, RUNTIME_ALL))
    found = {p: (r.kind, r.canonical) for p, r in snap.symbols.items()}
    for path in ("rtall.product", "rtall.core.product", "rtall.core.numeric.product"):
        assert found[path] == ("function", "rtall.core.fromnumeric.product")
    assert found["rtall.core.ones"] == ("function", "rtall.core.numeric.ones")
    # Names with nothing to read stay present, as unresolved aliases: compiled code, the standard library.
    assert found["rtall.core.numeric.dragon"] == ("alias", "rtall.core._compiled.dragon")
    assert found["rtall.dragon"] == ("alias", "rtall.core._compiled.dragon")
    assert found["rtall.lib.math"] == ("alias", "math")
    assert found["rtall.math"] == ("alias", "math")
    # Additive only: what was public stays public (the 2026-10-30 attempt lost `numpy.lib.NumpyVersion`).
    assert found["rtall.sum"] == ("function", "rtall.core.fromnumeric.sum")
    assert {"rtall.ArrayLike", "rtall.lib.NumpyVersion", "rtall.lib.tool", "rtall.tool"} <= set(found)
    # A star import griffe couldn't expand isn't a name (the index had `numpy.core.multiarray.numpy/core/...*`).
    assert not [p for p in found if "*" in p]


def test_overload_only_stub_functions_are_indexed(tmp_path):
    # DT-DATA-29: griffe keeps `@overload` signatures aside until an implementation follows, and a stub has none, so
    # `numpy.ndarray.reshape`, `astype` and `itemset` were missing from the index.
    snap = build_snapshot("overly", "1.0", ["overly"], _write(tmp_path, OVERLOADS))

    def sig(path: str) -> list[tuple[str, str, bool]]:
        rec = snap.symbols[path]
        assert rec.kind == "function", path
        return [(p["name"], p["kind"], p["default"] is None) for p in rec.params]

    # One signature that accepts every overload's call: a parameter is required only when every overload requires it.
    assert sig("overly.ndarray.reshape") == [
        ("shape", "positional-only", False),
        ("order", "keyword-only", False),
    ]
    assert sig("overly.ndarray.astype") == [  # inherited
        ("dtype", "positional or keyword", True),
        ("order", "positional or keyword", False),
        ("copy", "keyword-only", False),
        ("casting", "positional or keyword", False),
    ]
    # Annotated as griffe annotates an implementation from its overloads.
    assert [p["annotation"] for p in snap.symbols["overly.ndarray.astype"].params] == [
        "int | str",
        "str",
        "bool",
        "str",
    ]
    assert sig("overly.where") == [
        ("condition", "positional-only", True),
        ("x", "positional-only", False),
        ("y", "positional-only", False),
    ]
    assert sig("overly.ndarray.fill") == [("value", "positional or keyword", True)]
    # Deprecated only when every overload is: one deprecated call form isn't the function's deprecation.
    assert (
        "`ndarray.itemset` is deprecated, use `a[index] = value`"
        in snap.symbols["overly.ndarray.itemset"].deprecated
    )
    assert snap.symbols["overly.ndarray.item"].deprecated is None
    # With an implementation, its signature stands, as before.
    assert sig("overly.pick") == [
        ("x", "positional or keyword", True),
        ("strict", "positional or keyword", False),
        ("fallback", "keyword-only", False),
    ]


def test_the_else_branch_of_type_checking_is_runtime(tmp_path):
    # DT-DATA-26: griffe marks both branches as type-checking only, and `import *` skips names that aren't available
    # at runtime, so `pydantic.StrictBool` looked added in 2.0.
    snap = build_snapshot("guarded", "1.0", ["guarded"], _write(tmp_path, GUARDED))
    found = {p: (r.kind, r.canonical) for p, r in snap.symbols.items()}
    assert found["guarded.StrictBool"] == ("class", "guarded.types.StrictBool")
    assert found["guarded.ConstrainedInt"] == ("class", "guarded.types.ConstrainedInt")
    assert "guarded.Checker" not in found  # only type checkers see it


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


def test_lazy_exports_listed_only_in_a_computed_all(tmp_path):
    # DT-DATA-27: langchain 0.3.4's `langchain.chains.NatBotChain` and 47 more looked removed.
    snap = build_snapshot("shimmy", "1.0", ["shimmy"], _write(tmp_path, SHIMS))
    for name in ("NatBotChain", "APIChain"):
        rec = snap.symbols[f"shimmy.chains.{name}"]
        assert (rec.kind, rec.canonical, rec.deprecated) == ("getattr", "shimmy.chains.__getattr__", None)
    # A module without `__all__` doesn't say which names its catch-all `__getattr__` serves.
    assert not any(r.kind == "getattr" for p, r in snap.symbols.items() if p.startswith("shimmy.proxy."))


def test_a_getattr_that_forwards_to_a_module_serves_its_names(tmp_path):
    # DT-DATA-25: numpy 2.0's `numpy.core.*` forwarders serve `numpy._core.*`; 1,198 names looked removed in 2.0.0.
    snap = build_snapshot("fwd", "1.0", ["fwd"], _write(tmp_path, FORWARDS))
    equiv = snap.symbols["fwd.core.numeric.array_equiv"]
    assert (equiv.kind, equiv.canonical) == ("function", "fwd._core.numeric.array_equiv")
    assert [p["name"] for p in equiv.params] == ["a1", "a2"]
    # The warning comes from the helper, with its arguments bound.
    assert equiv.deprecated == (
        "fwd.core.numeric is deprecated and has been renamed to fwd._core.numeric. use fwd._core.numeric.array_equiv."
    )
    # A forwarded class, but not its members: griffe resolves numpy's private `_core` stubs differently from one
    # release to the next (an alias cycle on `ndarray` from 2.2), and members came and went with it.
    assert snap.symbols["fwd.core.numeric.ndarray"].kind == "class"
    assert not [p for p in snap.symbols if p.startswith("fwd.core.numeric.ndarray.")]
    # The package forwards to the package, and its helper call names no submodule.
    assert snap.symbols["fwd.core.array_equiv"].deprecated == (
        "fwd.core is deprecated and has been renamed to fwd._core. use fwd._core.array_equiv."
    )
    # A real submodule stays itself; a name the module copies in itself doesn't warn.
    numeric = snap.symbols["fwd.core.numeric"]
    assert (numeric.kind, numeric.canonical, numeric.deprecated) == ("module", "fwd.core.numeric", None)
    scalar = snap.symbols["fwd.core.numeric.scalar"]
    assert (scalar.canonical, scalar.deprecated) == ("fwd._core.numeric.scalar", None)
    # Private names aren't served; a target with no source serves nothing.
    assert not {"fwd.core.numeric._private", "fwd.core._private"} & set(snap.symbols)
    assert {p for p in snap.symbols if p.startswith("fwd.core.umath.")} == {"fwd.core.umath.__getattr__"}


def test_a_getattr_that_looks_names_up_in_a_table_serves_its_keys(tmp_path):
    # DT-DATA-25: numpy 1.25-1.26's `np.math` and 1.24-1.26's `np.bool8`, `np.int0`... live in a table its
    # `__getattr__` reads in a `try`; django 6.1's `core.mail.BadHeaderError` looked removed.
    snap = build_snapshot("tabled", "1.0", ["tabled"], _write(tmp_path, TABLES))
    bool8 = snap.symbols["tabled.bool8"]
    assert (bool8.kind, bool8.canonical) == ("getattr", "tabled.__getattr__")
    assert bool8.deprecated == "`np.bool8` is a deprecated alias for `np.bool_`.  (Deprecated NumPy 1.24)"
    assert snap.symbols["tabled.int0"].deprecated == (
        "`np.int0` is a deprecated alias for `np.intp`.  (Deprecated NumPy 1.24)"
    )
    # `str.format` fills only fields its arguments name; text a value brings along stays as it is.
    assert snap.symbols["tabled.str0"].deprecated == (
        "`np.str0` is a deprecated alias for `{0}`.  (Deprecated NumPy 1.24)"
    )
    # The module imports `math` and deletes it again, so the name reaches `__getattr__`.
    math = snap.symbols["tabled.math"]
    assert (math.kind, math.deprecated) == (
        "getattr",
        "`np.math` is a deprecated alias for the standard library `math` module (Deprecated Numpy 1.25). "
        "Replace usages of `np.math` with `math`",
    )
    # A dummy that raises when called (numpy's expired financial functions) and a raising branch serve nothing.
    assert not {"tabled.fv", "tabled.pmt", "tabled.object", "tabled.bool"} & set(snap.symbols)
    header = snap.symbols["tabled.mail.BadHeaderError"]
    assert (header.kind, header.deprecated) == (
        "getattr",
        "BadHeaderError is deprecated. Replace with ValueError.",
    )
    assert snap.symbols["tabled.mail.SafeMIMEText"].deprecated == "SafeMIMEText is deprecated."
    assert snap.symbols["tabled.mail.EmailMessage"].kind == "class"
    thing = snap.symbols["tabled.lazy.Thing"]
    assert (thing.kind, thing.deprecated) == ("getattr", None)


def test_a_subpackage_that_star_imports_its_package_gets_all_of_it(tmp_path):
    # DT-DATA-33: griffe merges a package's own star imports only after it has expanded its submodules', so
    # pydantic 1.10.17-1.10.26's `pydantic.v1` got only the names `pydantic` imports by name, and
    # `pydantic.v1.BaseModel` read removed in 1.10.17 and added again in 2.0.
    snap = build_snapshot("pstar", "1.0", ["pstar"], _write(tmp_path, PARENT_STAR))
    model = snap.symbols["pstar.v1.BaseModel"]
    assert (model.kind, model.canonical) == ("class", "pstar.main.BaseModel")
    assert "pstar.v1.BaseModel.dict" in snap.symbols
    create = snap.symbols["pstar.v1.create_model"]
    assert (create.kind, create.canonical) == ("function", "pstar.main.create_model")
    assert [p["name"] for p in create.params] == ["name", "fields"]
    assert snap.symbols["pstar.v1.BaseConfig"].canonical == "pstar.config.BaseConfig"
    # Only what `from pstar import *` binds: `pstar.__all__`.
    assert "pstar.v1.helper" not in snap.symbols
    # What griffe expanded already is unchanged.
    assert snap.symbols["pstar.v1.main.BaseModel"].canonical == "pstar.main.BaseModel"
    assert "pstar.v1.main.helper" not in snap.symbols


def test_a_module_whose_top_level_always_raises_is_absent(tmp_path):
    # DT-DATA-34: mcp 2.x's `mcp.server.fastmcp` placeholder read present, so "No module named
    # 'mcp.server.fastmcp'" on mcp 2.3.0 got no "removed in 2.0.0" answer.
    snap = build_snapshot("rpkg", "2.0", ["rpkg"], _write(tmp_path, RAISES))
    assert not [p for p in snap.symbols if p.startswith("rpkg.server.legacy")]
    # Nothing below a package that raises can be imported either.
    assert not [p for p in snap.symbols if p.startswith("rpkg.types")]
    assert {"rpkg", "rpkg.Server", "rpkg.server", "rpkg.server.Server.run"} <= set(snap.symbols)
    # A raise under `if` or `try` is conditional, and one inside a function runs only when it is called.
    assert {"rpkg.optional", "rpkg.optional.fast", "rpkg.gated", "rpkg.gated.run"} <= set(snap.symbols)


def test_folders_without_init_load_as_packages(tmp_path):
    # DT-DATA-23: litellm's `types` and most of `llms` have no `__init__.py`, so `litellm.types.utils.ModelResponse`
    # and the top-level names `litellm/__init__.py` star-imports from there read absent.
    snap = build_snapshot(
        "nspkg", "1.0", ["nspkg"], _write(tmp_path, NAMESPACES), namespace_skip=["nspkg.proxy"]
    )
    assert {
        "nspkg.types.utils.ModelResponse",
        "nspkg.ModelResponse",
        "nspkg.Model",
        "nspkg.llms.base.Model.run",
        "nspkg.llms.openai.chat.complete",
        "nspkg.contrib.real.tool",
        "nspkg.proxy.serve",
    } <= set(snap.symbols)
    for absent in ("nspkg.proxy.routes", "nspkg.tests", "nspkg.static", "nspkg.bad"):
        assert not [p for p in snap.symbols if p.startswith(absent)], absent
    # `shadow.py` stays the module. (griffe itself attaches `shadow/inner.py` to it, as it did before.)
    assert snap.symbols["nspkg.shadow.g"].kind == "function"
    made = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("__init__.py") if not p.read_text())
    assert made == [
        "nspkg/contrib/__init__.py",
        "nspkg/llms/__init__.py",
        "nspkg/llms/openai/__init__.py",
        "nspkg/types/__init__.py",
    ]
    assert snap.namespace_skip == ["nspkg.proxy"]


def test_function_warnings_with_a_removed_in_category_are_deprecations(tmp_path):
    # DT-DATA-36: django 3.2's `force_text` warns with `RemovedInDjango40Warning` and read as not deprecated.
    snap = build_snapshot("djangoish", "3.2", ["djangoish"], _write(tmp_path, REMOVED_IN))
    assert (
        snap.symbols["djangoish.force_text"].deprecated
        == "force_text() is deprecated in favor of force_str()."
    )
    assert snap.symbols["djangoish.force_str"].deprecated is None


def test_every_warning_in_a_function_is_read(tmp_path):
    # DT-DATA-42: each warning counts. One that a condition on an argument guards, and whose text names that
    # argument, deprecates the argument; one emitted unconditionally deprecates the function.
    snap = build_snapshot("warny", "2.0", ["warny"], _write(tmp_path, WARNINGS))

    def dep(path):
        rec = snap.symbols[f"warny.{path}"]
        return rec.deprecated, rec.deprecated_params

    assert dep("validator") == (
        "V1 style `@validator` validators are deprecated. You should migrate to V2 style `@field_validator`"
        " validators.",
        {"allow_reuse": "`allow_reuse` is deprecated and does nothing."},
    )
    # A raise isn't a warning, and `extra` (the **kwargs itself) isn't an argument anyone passes by name.
    assert dep("Field") == (
        None,
        {"min_items": "`min_items` is deprecated and will be removed, use `min_length` instead"},
    )
    # Names taken out of **kwargs count; a warning about leaving an argument out doesn't deprecate it.
    assert dep("StateGraph.__init__") == (
        None,
        {
            "config_schema": "`config_schema` is deprecated and will be removed. Please use `context_schema` instead."
        },
    )
    assert dep("Agent.__init__") == (
        None,
        {"result_type": "`result_type` is deprecated, use `output_type` instead"},
    )
    # A warning without a category counts when its text says "deprecated".
    assert dep("Interface.__init__") == (
        None,
        {
            "allow_flagging": "The `allow_flagging` parameter in `Interface` is deprecated.Use `flagging_mode` instead."
        },
    )
    # *args isn't a parameter to deprecate either; a bare mention of the argument is enough under its condition.
    assert dep("CheckConstraint.__init__") == (
        None,
        {"check": "CheckConstraint.check is deprecated in favor of `.condition`."},
    )
    assert dep("old_api") == ("old_api() is deprecated, use new_api() instead", None)
    assert dep("unreadable") == ("deprecated (warning emitted; message not statically recoverable)", None)
    # Unreadable text under a condition on an argument: which argument it deprecates is unknown, so nothing.
    assert dep("unreadable_param") == (None, None)
    # Warnings in nested functions are their own; filtering warnings isn't warning.
    assert dep("deprecate_kwarg") == (None, None)
    assert dep("quiet") == (None, None)
    # The text a name holds where the warning is emitted, not where it was last bound. A warning for some values
    # of an argument (`isinstance(verify, str)`) doesn't deprecate the argument.
    assert dep("create_ssl_context") == (
        None,
        {"cert": "`cert=...` is deprecated. Use `verify=<ssl_context>` instead."},
    )
    # The loop variable `key` isn't the parameter `key`; the names it is compared with are deprecated kwargs.
    assert dep("BarPlot.__init__") == (
        None,
        {
            "show_actions_button": "Argument 'show_actions_button' has been deprecated.",
            "x_label_angle": "Argument 'x_label_angle' has been deprecated.",
        },
    )
    # A comparison with a sentinel default tests whether the argument was passed.
    assert dep("Retry.__init__") == (
        None,
        {
            "method_whitelist": "Using 'method_whitelist' with Retry is deprecated. Use 'allowed_methods' instead"
        },
    )
    # Setting an attribute from an argument doesn't make every later `if self.…` a condition on that argument.
    assert dep("Store.__init__") == ("Store is deprecated, use NewStore instead.", None)
    # A warning about another function's argument isn't this one's deprecation; one naming its own class is.
    assert dep("Encoder.fit") == (None, None)
    assert dep("Legacy.__init__") == ("`Legacy` is deprecated, use `Modern` instead.", None)
    assert dep("make_aware") == (None, {"is_dst": "The is_dst argument to make_aware() is deprecated."})
    assert dep("enable_chunked_encoding") == (None, {"chunk_size": "Chunk size is deprecated #1615"})
    assert dep("get_random_string") == (None, None)  # leaving it out is what's deprecated
    # The wording says which name is deprecated; the other is what to use instead, a placeholder isn't text.
    assert dep("d2_log_loss_score") == (
        None,
        {"y_pred": "`y_pred` was renamed to `y_proba` in version 1.9. Use `y_proba` instead."},
    )
    assert dep("endpoint") == (None, None)
    assert dep("ExclusionConstraint.__init__") == (
        None,
        {
            "opclasses": "The opclasses argument is deprecated in favor of using OpClass in"
            " ExclusionConstraint.expressions."
        },
    )
    assert dep("parse_all_fields") == (
        'Use pd.to_datetime({"year": year_col, "month": month_col}) instead.',
        None,
    )


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
