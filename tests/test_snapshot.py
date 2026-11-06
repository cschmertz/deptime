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


# DT-DATA-44: names a module's `__all__` leaves out. pandas' `api/types/__init__.py` star-imports the `is_*` functions
# and lists six other names; django's `contrib/postgres/indexes.py` defines `OpClass` and leaves it out; openai 1.x
# defines `api_key` in `__init__.py` and leaves it out.
UNLISTED = {
    "listed/__init__.py": "",
    "listed/api/__init__.py": (
        "from listed.core.dtypes import *\n"
        "from listed.core.plain import *\n"
        "from listed.core.dtypes import CategoricalDtype\n\n"
        "__all__ = ['CategoricalDtype']\n"
    ),
    "listed/core/__init__.py": "",
    "listed/core/dtypes.py": (
        "__all__ = ['is_float_dtype', 'CategoricalDtype']\n\n\n"
        "def is_float_dtype(arr):\n    pass\n\n\n"
        "def is_hidden(arr):\n    pass\n\n\n"
        "class CategoricalDtype:\n    pass\n"
    ),
    "listed/core/plain.py": (
        "from typing import Any\nfrom listed.core.dtypes import is_hidden\n\n\ndef made_here():\n    pass\n"
    ),
    "listed/indexes.py": (
        "from typing import TYPE_CHECKING\n"
        "from listed.core.dtypes import is_float_dtype\n\n"
        "__all__ = ['BTreeIndex']\n\n"
        "api_key: str | None = None\n"
        "timeout: float\n"
        "if TYPE_CHECKING:\n\n    class Hint:\n        pass\n\n\n"
        "try:\n"
        "    from listed._version import version as __version__\n"
        "except ImportError:\n"
        "    v = '1.0'\n"
        "    __version__ = v\n"
        "    del v\n\n\n"
        "class BTreeIndex:\n    pass\n\n\n"
        "class OpClass:\n    def __init__(self, expression, name):\n        pass\n\n\n"
        "def _private():\n    pass\n"
    ),
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
        "class SafeMIMEText:\n    def as_string(self):\n        pass\n"
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


# starlette 0.48's `status.__getattr__` and pandas 3.1's `api.typing.__getattr__` read tables they define themselves.
LOCAL_TABLES = {
    "localt/__init__.py": "",
    "localt/status.py": (
        "import warnings\n\n"
        "__all__ = ['HTTP_413_CONTENT_TOO_LARGE']\n\n"
        "HTTP_413_CONTENT_TOO_LARGE = 413\n\n"
        "__deprecated__ = {'HTTP_413_REQUEST_ENTITY_TOO_LARGE': 413, 'HTTP_000_ZERO': 0, 'HTTP_NONE': None}\n\n\n"
        "def __getattr__(name: str) -> int:\n"
        "    deprecation_changes = {\n"
        "        'HTTP_413_REQUEST_ENTITY_TOO_LARGE': 'HTTP_413_CONTENT_TOO_LARGE',\n"
        "    }\n\n"
        "    deprecated = __deprecated__.get(name)\n"
        "    if deprecated:\n"
        "        warnings.warn(\n"
        "            f\"'{name}' is deprecated. Use '{deprecation_changes[name]}' instead.\",\n"
        "            category=DeprecationWarning,\n"
        "            stacklevel=3,\n"
        "        )\n"
        "        return deprecated\n\n"
        "    raise AttributeError(f\"module 'localt.status' has no attribute '{name}'\")\n"
    ),
    "localt/window.py": "class RollingGroupBy:\n    pass\n",
    "localt/typing.py": (
        "from typing import TYPE_CHECKING\n\n"
        "from localt.window import RollingGroupBy\n\n"
        "__all__ = ['RollingGroupBy', 'Listed']\n\n"
        "if TYPE_CHECKING:\n"
        "    from localt.window import RollingGroupBy as Listed\n\n"
        "    RollingGroupby = RollingGroupBy\n\n\n"
        "def __getattr__(name: str) -> object:\n"
        "    deprecated = {'RollingGroupby': RollingGroupBy, 'Elsewhere': Unknown, 'Listed': RollingGroupBy}\n"
        "    if name in deprecated:\n"
        "        import warnings\n\n"
        "        new = deprecated[name]\n"
        "        warnings.warn(\n"
        "            f'localt.typing.{name} is deprecated. Use localt.typing.{new.__name__} instead.',\n"
        "            FutureWarning,\n"
        "        )\n"
        "        return new\n"
        "    raise AttributeError(name)\n"
    ),
    "localt/rename.py": (
        "import warnings\n\n"
        "_renamed = {'old_name': 'new_name', 'gone': None}\n"
        "new_name = 1\n\n\n"
        "def __getattr__(name):\n"
        "    if (new := _renamed.get(name)) is not None:\n"
        "        warnings.warn(f'{name} is deprecated, use {new} instead', DeprecationWarning)\n"
        "        return globals()[new]\n"
        "    raise AttributeError(name)\n"
    ),
    "localt/moved.py": (
        "import warnings\n\n"
        "_moved = {'Shadowed': 'localt.window.RollingGroupBy'}\n\n\n"
        "def __getattr__(name):\n"
        "    _moved = {'Mover': 'localt.window.RollingGroupBy'}\n"
        "    try:\n"
        "        target = _moved[name]\n"
        "    except KeyError:\n"
        "        raise AttributeError(name) from None\n"
        "    warnings.warn(f'{name} has moved to {target}', DeprecationWarning)\n"
        "    return target\n"
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


# DT-DATA-40: numpy 1.25-1.26 warn about `product`, `alltrue`... in the dispatcher their decorator is given, and the
# function itself only forwards to `prod`. pandas' `@doc(where)` passes a function for its docstring.
DISPATCHED = {
    "dispy/__init__.py": "from dispy.fromnumeric import *\nfrom dispy.frame import *\n",
    "dispy/fromnumeric.py": textwrap.dedent(
        '''\
        import functools
        import warnings

        from dispy import overrides

        __all__ = ["prod", "product", "sum"]

        array_function_dispatch = functools.partial(overrides.array_function_dispatch, module="dispy")


        def _prod_dispatcher(a, axis=None, dtype=None, out=None):
            return (a, out)


        @array_function_dispatch(_prod_dispatcher)
        def prod(a, axis=None, dtype=None, out=None):
            return a


        def _product_dispatcher(a, axis=None, dtype=None, out=None):
            # 2023-03-02, 1.25.0
            warnings.warn("`product` is deprecated as of NumPy 1.25.0, and will be "
                          "removed in NumPy 2.0. Please use `prod` instead.",
                          DeprecationWarning, stacklevel=3)
            return (a, out)


        @array_function_dispatch(_product_dispatcher, verify=False)
        def product(*args, **kwargs):
            """
            Return the product of array elements over a given axis.
            """
            return prod(*args, **kwargs)


        def _sum_dispatcher(a, axis=None, out=None):
            warnings.warn("passing `out` positionally is deprecated", DeprecationWarning)
            return (a, out)


        @array_function_dispatch(_sum_dispatcher)
        def sum(a, axis=None, out=None):
            return a
        '''
    ),
    "dispy/overrides.py": textwrap.dedent(
        """\
        def array_function_dispatch(dispatcher=None, module=None, verify=True):
            def decorator(implementation):
                return implementation
            return decorator
        """
    ),
    "dispy/frame.py": textwrap.dedent(
        """\
        import warnings

        __all__ = ["where", "mask", "lookup", "select"]


        def doc(*docstrings):
            def decorator(func):
                return func
            return decorator


        def where(cond, other=None, try_cast=None):
            if try_cast is not None:
                warnings.warn("try_cast keyword is deprecated and will be removed.", FutureWarning)
            return cond


        @doc(where)
        def mask(cond, other=None, try_cast=None):
            return where(cond, other, try_cast)


        def lookup(row_labels, col_labels):
            warnings.warn("lookup() is deprecated, use select() instead.", FutureWarning)
            return row_labels


        @doc(lookup)
        def select(row_labels, col_labels):
            return row_labels
        """
    ),
}


# DT-DATA-39: an argument a function passes on, under its own name, to a function of its package that deprecates it:
# anthropic's client passes `proxies` to its base class's `__init__`, httpx's `Client` passes `cert` three calls
# down to `create_ssl_context`, fastapi's `Query()` builds `params.Query`, whose base class warns, and pytest's
# `skip()` hands `msg` to a helper that warns with a module constant from another module.
PASSED = {
    "passy/__init__.py": textwrap.dedent(
        """\
        from passy._client import Client
        from passy.functions import (
            Frame, Mounts, Query, exit, fail, mask, resample, skip, timed, where, write_line, yield_fixture,
        )
        from passy.routing import App, Router, StartupOnly
        from passy.fields import IP, Field, Filtered, Strict
        from passy import params
        """
    ),
    "passy/_base.py": textwrap.dedent(
        """\
        import warnings


        class BaseClient:
            def __init__(self, *, base_url, proxies=None, timeout=None, verify=True, filter=None, issuer=None):
                if proxies is not None:
                    warnings.warn(
                        "The `proxies` argument is deprecated. The `http_client` argument should be passed instead",
                        DeprecationWarning,
                    )
                if timeout == "auto":
                    warnings.warn("timeout='auto' is deprecated", DeprecationWarning)
                if isinstance(filter, dict):
                    warnings.warn("Using dict as a `filter` is deprecated.", DeprecationWarning)
                if issuer is None:
                    warnings.warn("Omitting `issuer` is deprecated and it will be required in 3.0.", DeprecationWarning)
        """
    ),
    "passy/_config.py": textwrap.dedent(
        """\
        import warnings


        def create_context(verify=True, cert=None):
            if isinstance(verify, str):
                warnings.warn("`verify=<str>` is deprecated.", DeprecationWarning)
            if cert:
                message = (
                    "`cert=...` is deprecated. Use `verify=<ssl_context>` instead,"
                    "with `.load_cert_chain()` to configure the certificate chain."
                )
                warnings.warn(message, DeprecationWarning)
            return verify
        """
    ),
    "passy/_transports.py": textwrap.dedent(
        """\
        from ._config import create_context


        class Transport:
            def __init__(self, verify=True, cert=None):
                self.ctx = create_context(verify=verify, cert=cert)
        """
    ),
    "passy/_client.py": textwrap.dedent(
        """\
        from ._base import BaseClient
        from ._transports import Transport


        class Client(BaseClient):
            def __init__(
                self, *, base_url="", proxies=None, timeout=None, verify=True, cert=None, client_cert=None, filter=None,
                issuer=None,
            ):
                super().__init__(
                    base_url=base_url, proxies=proxies, timeout=timeout, verify=verify, filter=filter, issuer=issuer
                )
                self._transport = self._init_transport(verify=verify, cert=cert)
                self._mounts = self._init_transport(verify, client_cert)

            def _init_transport(self, verify=True, cert=None):
                return Transport(verify=verify, cert=cert)

            def ping(self, proxies=None):
                return proxies
        """
    ),
    "passy/params.py": textwrap.dedent(
        """\
        import warnings


        class Param:
            def __init__(self, default=None, *, regex=None, pattern=None):
                if regex is not None:
                    warnings.warn("`regex` has been deprecated, please use `pattern` instead", DeprecationWarning)


        class Query(Param):
            def __init__(self, default=None, *, regex=None, pattern=None):
                super().__init__(default, regex=regex, pattern=pattern)
        """
    ),
    "passy/deprecated.py": textwrap.dedent(
        """\
        class PytestDeprecationWarning(DeprecationWarning):
            pass


        class PytestRemovedIn8Warning(PytestDeprecationWarning):
            pass


        class UnformattedWarning:
            def __init__(self, category, template):
                self.category, self.template = category, template


        KEYWORD_MSG_ARG = UnformattedWarning(
            PytestRemovedIn8Warning,
            "pytest.{func}(msg=...) is now deprecated, use pytest.{func}(reason=...) instead",
        )
        WRITE_LINE_BYTES = UnformattedWarning(
            PytestRemovedIn8Warning,
            "Passing bytes to write_line() is deprecated, pass a str instead.\\n"
            "See https://docs.example.org/deprecations.html#write-line-bytes",
        )
        YIELD_FIXTURE = PytestDeprecationWarning(
            "@pytest.yield_fixture is deprecated.\\nUse @pytest.fixture instead; they are the same."
        )
        """
    ),
    "passy/_outcomes.py": textwrap.dedent(
        """\
        import warnings

        from passy.deprecated import KEYWORD_MSG_ARG


        def _resolve_msg_to_reason(func_name, reason, msg=None):
            if msg is not None:
                if reason:
                    raise ValueError("Passing both ``reason`` and ``msg`` is not permitted.")
                warnings.warn(KEYWORD_MSG_ARG.format(func=func_name), stacklevel=3)
                reason = msg
            return reason


        def _where(cond, other=None, try_cast=None):
            if try_cast is not None:
                warnings.warn("try_cast keyword is deprecated and will be removed.", FutureWarning)
            return cond


        def _resample(obj, rule, base=None):
            '''Resample.

            Parameters
            ----------
            base : int
                .. deprecated:: 1.1.0
                   The new arguments that you should use are 'offset' or 'origin'.
            '''
            return obj


        def describe_ndframe(*, obj, include, exclude):
            if obj.ndim == 1:
                if include is not None or exclude is not None:
                    warnings.warn("The 'include' and 'exclude' arguments are deprecated for Series.describe", FutureWarning)
            return obj


        def _a(x, depth=None):
            return _b(x, depth=depth)


        def _b(x, depth=None):
            return _c(x, depth=depth)


        def _c(x, depth=None):
            return _d(x, depth=depth)


        def _d(x, depth=None):
            if depth is not None:
                warnings.warn("`depth` is deprecated", DeprecationWarning)
            return _a(x, depth=depth)
        """
    ),
    "passy/functions.py": textwrap.dedent(
        """\
        import warnings

        from passy import params
        from passy._outcomes import _a, _resample, _resolve_msg_to_reason, _where, describe_ndframe
        from passy._transports import Transport
        from passy.deprecated import KEYWORD_MSG_ARG, WRITE_LINE_BYTES, YIELD_FIXTURE


        def Query(default=None, *, regex=None, pattern=None):
            return params.Query(default, regex=regex, pattern=pattern)


        def skip(reason="", *, allow_module_level=False, msg=None):
            reason = _resolve_msg_to_reason("skip", reason, msg)


        def fail(reason="", pytrace=True, msg=None):
            reason = _resolve_msg_to_reason("fail", reason, msg)


        def exit(reason="", returncode=None, *, msg=None):
            if msg is not None:
                warnings.warn(KEYWORD_MSG_ARG.format(func="exit"), stacklevel=2)


        def yield_fixture(fixture_function=None, *args, scope="function"):
            warnings.warn(YIELD_FIXTURE, stacklevel=2)


        def mask(cond, other=None, try_cast=None):
            return _where(cond, other, try_cast)


        def timed(x, depth=None):
            return _a(x, depth=depth)


        def where(cond, other=None, try_cast=None):
            '''Replace values where the condition is False.

            Parameters
            ----------
            try_cast : bool
                .. deprecated:: 1.3.0
                   Manually cast back if necessary.
            '''
            return _where(cond, other, try_cast)


        def resample(obj, rule, base=None):
            return _resample(obj, rule, base=base)


        class Mounts:
            def __init__(self, client_cert=None):
                self.transport = Transport(True, client_cert)


        def write_line(line, **markup):
            if not isinstance(line, str):
                warnings.warn(WRITE_LINE_BYTES.format(), stacklevel=2)


        class Frame:
            def describe(self, include=None, exclude=None):
                return describe_ndframe(obj=self, include=include, exclude=exclude)
        """
    ),
    "passy/fields.py": textwrap.dedent(
        """\
        import warnings


        class Field:
            def __init__(self, *, load_default=None, missing=None, **metadata):
                if missing is not None:
                    warnings.warn(
                        "The 'missing' argument to fields is deprecated. Use 'load_default' instead.",
                        DeprecationWarning,
                    )


        class IP(Field):
            def __init__(self, *args, exploded=False, **kwargs):
                super().__init__(*args, **kwargs)


        class Strict(Field):
            def __init__(self, **kwargs):
                if "missing" in kwargs:
                    raise TypeError("no default for strict fields")
                super().__init__(**kwargs)


        class Filtered(Field):
            def __init__(self, **kwargs):
                kwargs = {k: v for k, v in kwargs.items() if k != "missing"}
                super().__init__(**kwargs)
        """
    ),
    "passy/routing.py": textwrap.dedent(
        """\
        import warnings


        class Router:
            def __init__(self, routes=None, on_startup=None, on_shutdown=None, lifespan=None):
                if on_startup or on_shutdown:
                    warnings.warn(
                        "The on_startup and on_shutdown parameters are deprecated, and they will be removed on "
                        "version 1.0. Use the lifespan parameter instead.",
                        DeprecationWarning,
                    )


        class App:
            def __init__(self, routes=None, on_startup=None, on_shutdown=None, lifespan=None):
                self.router = Router(routes, on_startup=on_startup, on_shutdown=on_shutdown, lifespan=lifespan)


        class StartupOnly:
            def __init__(self, on_startup=None):
                self.router = Router(on_startup=on_startup)
        """
    ),
}


# DT-DATA-37: deprecation helpers, recognized by name. Decorators (polars, redis, huggingface-hub, matplotlib,
# sqlalchemy, langgraph, pandas, weaviate), wrapper calls that bind the old name (streamlit, pandas, matplotlib's
# class attributes) and warning helpers called in a body (polars, matplotlib, streamlit).
HELPERS = {
    "helpy/__init__.py": textwrap.dedent(
        """\
        from helpy._deprecation import deprecate_func_name as _deprecate_func_name
        from helpy.api import _experimental_rerun
        from helpy.execution import get_query_params as _get_query_params
        from helpy.execution import rerun as _rerun

        _QP_MSG = "Refer to the docs page for more information."

        experimental_rerun = _deprecate_func_name(_rerun, "experimental_rerun", "2024-04-01")
        older_rerun = _experimental_rerun
        experimental_get_query_params = _deprecate_func_name(
            _get_query_params,
            "experimental_get_query_params",
            "2024-04-11",
            _QP_MSG,
            name_override="query_params",
        )
        """
    ),
    "helpy/execution.py": "def rerun():\n    pass\n\n\ndef get_query_params():\n    return {}\n",
    "helpy/_deprecation.py": textwrap.dedent(
        """\
        import functools
        import warnings


        def _passthrough(*args, **kwargs):
            def decorate(function):
                @functools.wraps(function)
                def wrapper(*a, **k):
                    warnings.warn("deprecated", DeprecationWarning)
                    return function(*a, **k)
                return wrapper
            return decorate


        deprecate_function = deprecate_renamed_function = deprecate_renamed_parameter = _passthrough
        deprecate_nonkeyword_arguments = deprecated_function = deprecated_args = _passthrough
        _deprecate_method = _deprecate_arguments = deprecate_kwarg = deprecated_parameter = _passthrough


        def deprecate_func_name(func, old_name, removal_date, extra_message=None, name_override=None):
            return func


        def deprecate(name, alternative, version, alt_name=None):
            return alternative


        def show_deprecation_warning(message, show_in_browser=True):
            warnings.warn(message, DeprecationWarning)


        def make_deprecated_name_warning(old_name, new_name, removal_date, extra_message=None, include_st_prefix=True):
            return f"Please replace `{old_name}` with `{new_name}`."
        """
    ),
    "helpy/utils.py": textwrap.dedent(
        """\
        import warnings


        def warn_deprecated(name, reason=""):
            warnings.warn(f"Call to deprecated {name}. ({reason})", DeprecationWarning)


        def issue_deprecation_warning(message, *, version):
            warnings.warn(message, DeprecationWarning)


        class Pandas4Warning(DeprecationWarning):
            pass
        """
    ),
    "helpy/io.py": textwrap.dedent(
        """\
        from helpy._deprecation import deprecate


        def _json_normalize(data):
            return data


        json_normalize = deprecate("helpy.io.json_normalize", _json_normalize, "1.0.0", "helpy.json_normalize")
        """
    ),
    "helpy/api.py": textwrap.dedent(
        """\
        import logging

        from typing_extensions import deprecated as typing_deprecated

        from helpy import _api, util
        from helpy._deprecation import (
            _deprecate_arguments,
            _deprecate_method,
            deprecate_function,
            deprecate_kwarg,
            deprecate_nonkeyword_arguments,
            deprecate_renamed_function,
            deprecate_renamed_parameter,
            deprecated_args,
            deprecated_function,
            deprecated_parameter,
            make_deprecated_name_warning,
            show_deprecation_warning,
        )
        from helpy.execution import rerun
        from helpy.utils import Pandas4Warning, issue_deprecation_warning

        _HF_MSG = "Use `hf_hub_download` instead."
        logger = logging.getLogger(__name__)


        class Expr:
            def map_elements(self, function):
                return function

            @deprecate_renamed_function("map_elements", version="0.19.0")
            def apply(self, function):
                return self.map_elements(function)

            @deprecate_renamed_function("name.map", version="0.19.12", moved=True)
            def map_alias(self, function):
                return function

            @deprecate_function("Set the ordering on the data type instead.", version="0.19.19")
            def set_ordering(self, ordering):
                return ordering

            @deprecate_renamed_parameter("periods", "n", version="0.19.11")
            def shift(self, n=1):
                return n

            @deprecate_nonkeyword_arguments(allowed_args=["self"], version="0.20")
            def sort(self, descending=False):
                return descending

            def clip(self, lower=None, min_val=None):
                if min_val is not None:
                    issue_deprecation_warning("`min_val` is deprecated, use `lower` instead.", version="0.19.0")
                return lower


        class Redis:
            @deprecated_args(
                args_to_warn=["retry_on_timeout"], reason="TimeoutError is included by default.", version="6.0.0"
            )
            def __init__(self, host="localhost", retry_on_timeout=False):
                self.host = host

            @deprecated_function(version="5.0.1", reason="Use aclose() instead", name="close")
            async def close(self, close_connection_pool=None):
                pass

            async def aclose(self):
                pass


        @_deprecate_method(version="0.26", message=_HF_MSG)
        def cached_download(url, cache_dir=None):
            return url


        class InferenceApi:
            @_deprecate_method(version="1.0", message="Use `InferenceClient` instead.")
            def __init__(self, repo_id):
                self.repo_id = repo_id


        @_deprecate_arguments(version="0.14", deprecated_args={"cardData"}, custom_message="Use 'full' instead.")
        def list_datasets(full=None, cardData=None):
            return full


        class Colorbar:
            patch = _api.deprecate_privatize_attribute("3.5", alternative="ax")
            ax = None

            @_api.rename_parameter("3.6", "b", "visible")
            def grid(self, visible=None, which="major"):
                pass

            @_api.delete_parameter("3.6", "renderer")
            def get_tightbbox(self, renderer=None):
                pass

            def set_label(self, label, loc=None, beginarrow=None):
                if beginarrow is not None:
                    _api.warn_deprecated("3.5", name="beginarrow", alternative="arrow")

            def resize(self, w, h):
                _api.warn_deprecated("3.6", name="resize", obj_type="method", alternative="FigureManagerBase.resize")

            def clabel(self, levels=None):
                if self.filled:
                    _api.warn_deprecated("3.5", message="Applying clabel() to filled contours is deprecated.")

            def bxp(self, bxpstats, vert=None):
                if self.rc:
                    _api.warn_deprecated("3.10", name="boxplot.vertical", obj_type="rcparam")
                if vert is not None:
                    _api.warn_deprecated("3.11", name="vert: bool", alternative="orientation: {'vertical', 'horizontal'}")


        class MetaData:
            @util.deprecated_params(
                bind=(
                    "2.0",
                    "The :paramref:`.MetaData.bind` argument is deprecated and will be removed in SQLAlchemy 2.0.",
                )
            )
            def __init__(self, bind=None, schema=None):
                pass


        class Query:
            @util.became_legacy_20(
                ":meth:`_orm.Query.get`", alternative="The method is now available as :meth:`_orm.Session.get`"
            )
            def get(self, ident):
                pass


        @util.deprecated_20("relation", "Please use :func:`.relationship`.")
        def relation(*arg, **kw):
            pass


        @util.deprecated_cls(
            "1.4",
            "The :class:`.MapperOption` class is deprecated and will be removed in a future release.",
            constructor=None,
        )
        class MapperOption:
            pass


        @typing_deprecated("This method is deprecated. Use `connect_to_weaviate_cloud` instead.")
        def connect_to_wcs(cluster_url):
            return cluster_url


        def image(image, use_column_width=None, use_container_width=False):
            if use_column_width is not None:
                show_deprecation_warning(
                    "The `use_column_width` parameter has been deprecated and will be removed in a future release. "
                    "Please utilize the `use_container_width` parameter instead."
                )


        def dataframe(data, use_container_width=None, width="stretch"):
            if use_container_width is not None:
                show_deprecation_warning(
                    make_deprecated_name_warning(
                        "use_container_width",
                        "width",
                        "2025-12-31",
                        "For `use_container_width=True`, use `width='stretch'`.",
                        include_st_prefix=False,
                    ),
                    show_in_browser=False,
                )


        def _experimental_rerun():
            msg = make_deprecated_name_warning("experimental_rerun", "rerun", "2024-04-01")
            # Logged, not warned (streamlit 1.36).
            logger.warning(msg)
            rerun()


        @deprecated_parameter("messages_modifier", "0.1.9", "state_modifier", removal="0.3.0")
        def create_react_agent(model, tools, state_modifier=None):
            return model


        @deprecate_kwarg(old_arg_name="verbose", new_arg_name=None)
        def to_excel(excel_writer, verbose=True):
            return excel_writer


        @deprecate_kwarg(Pandas4Warning, "method", new_arg_name=None)
        def reindex_like(other, method=None):
            return other
        """
    ),
    "helpy/_api.py": "def deprecate_privatize_attribute(since, **kwargs):\n    return property()\n",
    "helpy/util.py": "",
}


# DT-DATA-38: deprecations a docstring's `.. deprecated::` note or a `'deprecated'` default states, in the shapes
# the covered packages use: scikit-learn documents its constructor parameters in the class docstring, numpy and
# pandas give a version only, polars notes renames at the top, sqlalchemy names a parameter with `:paramref:`.
DOCNOTES = {
    "docky/__init__.py": "from docky.api import *\n",
    "docky/api.py": textwrap.dedent(
        '''\
        import warnings

        __all__ = ["OneHotEncoder", "CodeAct", "percentile", "find_idx_by_name", "to_pydatetime", "Enum",
                   "connect", "warned", "unreadable", "future_axis", "Session", "create_engine", "binary_repr",
                   "to_excel", "SGDClassifier", "yield_fixture", "and_", "Client", "ElasticKnnSearch",
                   "LanguageAuto", "A2AConfig", "helpers", "names"]


        class OneHotEncoder:
            """Encode categorical features as a one-hot numeric array.

            Parameters
            ----------
            sparse : bool, default=True
                Will return sparse matrix if set True else will return an array.

                .. deprecated:: 1.2
                   `sparse` is deprecated in 1.2 and will be removed in 1.4. Use
                   `sparse_output` instead.

            sparse_output : bool, default=True
                Will return sparse matrix if set True.

            max_features : {"sqrt", "log2", None}, int or float, default=1.0
                The number of features to consider.

                .. deprecated:: 1.1
                    The `"auto"` option was deprecated in 1.1 and will be removed
                    in 1.3.

            loss : {'log_loss', 'deviance'}, default='log_loss'
                The loss function to be optimized.

                .. deprecated:: 1.1
                    The loss 'deviance' was deprecated in v1.1 and will be removed in
                    version 1.3. Use `loss='log_loss'` which is equivalent.

            base_estimator : object, default="deprecated"
                Use `estimator` instead.

            estimator : object, default=None
                The base estimator.

            use_legacy : bool, default=True
                .. deprecated:: 1.10
                   `use_legacy` will be deprecated in version 1.10 and be removed in 1.12.

            random_state : int, default=None
                ..deprecated:: 1.9
                  The `random_state` parameter is deprecated and will be removed in 1.11.

            Attributes
            ----------
            n_features_ : int
                .. deprecated:: 1.0
                   `n_features_` is deprecated and will be removed in 1.2.
            """

            def __init__(self, sparse="deprecated", sparse_output=True, max_features=1.0, loss="log_loss",
                         base_estimator="deprecated", estimator=None, use_legacy=True, random_state=None):
                self.sparse = sparse


        class CodeAct:
            """Run code as actions.

            .. deprecated:: 3.4
                CodeAct is deprecated. RLM is the preferred replacement.
            """

            def __init__(self, signature):
                self.signature = signature


        def percentile(a, q, axis=None, interpolation=None, *, kth=None, **kwargs):
            """
            Compute the q-th percentile of the data along the specified axis.

            Parameters
            ----------
            a : array_like
                Input array.
            interpolation : str, optional
                Deprecated name for the method keyword argument.

                .. deprecated:: 1.22.0
            kth : int or sequence of ints
                Element index to partition by.

                .. deprecated:: 1.22.0
                    Passing booleans as index is deprecated.
            **kwargs
                .. deprecated:: 3.1.0
                    Passing ``**kwargs`` is deprecated.
            """
            return a


        def find_idx_by_name(name):
            """
            Find the index of a column by name.

            .. deprecated:: 0.19.14
                This method has been renamed to :func:`get_column_index`.
            """
            return name


        def to_pydatetime(values):
            """
            Return the data as an array of datetime objects.

            .. deprecated:: 2.1.0
                The current behavior of dt.to_pydatetime is deprecated. In a future version this will
                return a Series containing python datetime objects instead of a ndarray.
            """
            return values


        class Enum:
            def __init__(self, *enums, inherit_schema=False, name=None):
                """Construct an enum.

                .. deprecated:: 2.1  Setting the :paramref:`.Enum.inherit_schema` parameter is deprecated.
                """


        def connect(host, timeout=None, retries=3):
            """Connect to a host.

            Args:
                host: The host.
                timeout (float): Seconds to wait.

                    .. deprecated:: 2.0
                        Use ``retries`` instead.
                retries (int): How many times to try.
            """
            return host


        def warned(old=None, new=None):
            """Read a value.

            Parameters
            ----------
            old : str
                .. deprecated:: 2.0
                   Use `new`.
            """
            if old is not None:
                warnings.warn("'old' is deprecated, use 'new' instead", DeprecationWarning)
            return new


        def unreadable(x):
            """Read x.

            .. deprecated:: 1.5
               Use ``readable`` instead.
            """
            warnings.warn(make_message(x), DeprecationWarning)
            return x


        def future_axis(axis=None):
            """
            .. deprecated:: 3.11
               The *axis* parameter is pending-deprecated. It will be deprecated in 3.13.
            """
            return axis


        class Session:
            def flush(self, objects=None):
                """Flush all the object changes to the database.

                :param objects: Optional; restricts the flush operation to operate
                  only on elements that are in the given collection.

                  .. deprecated:: 2.1
                """

            def distinct(self, *expr):
                r"""Apply a ``DISTINCT`` to the query.

                .. note::

                    The ORM-level :meth:`.distinct` call includes logic that will
                    automatically add columns from the ORDER BY of the query.

                    .. deprecated:: 2.0  This logic is deprecated and will be removed
                       in SQLAlchemy 2.0.

                :param \\*expr: optional column expressions.

                 .. deprecated:: 1.4 Using \\*expr in other dialects is deprecated
                    and will raise :class:`_exc.CompileError` in a future version.
                """


        def create_engine(url, **kwargs):
            """Create an engine.

            :param convert_unicode=False: if set to True, causes
                all String datatypes to act as though the
                :paramref:`.String.convert_unicode` flag has been set to ``True``.

                .. deprecated:: 1.3

                    The :paramref:`_sa.create_engine.convert_unicode` parameter
                    is deprecated and will be removed in a future release.

            :param echo=False: if True, the Engine will log all statements.
            """


        def binary_repr(num, width=None):
            """
            Return the binary representation of the input number as a string.

            Parameters
            ----------
            num : int
                Only an integer decimal number can be used.
            width : int, optional
                The length of the returned string. If the width value is insufficient, it will be
                ignored. This behavior is deprecated and will later raise an error.

                .. deprecated:: 1.12.0
            """


        def to_excel(excel_writer, engine=None, times=None):
            """
            Write object to an Excel sheet.

            Parameters
            ----------
            engine : str, optional
                Write engine to use, 'openpyxl' or 'xlsxwriter'.

                .. deprecated:: 1.2.0

                    As the `xlwt <https://pypi.org/project/xlwt/>`__ package is no longer
                    maintained, the ``xlwt`` engine will be removed in a future version
                    of pandas.
            times : str, np.ndarray, Series, default None
                Times corresponding to the observations.

                .. deprecated:: 1.4.0

                    If str, the name of the column in the DataFrame representing the times.
            """


        class SGDClassifier:
            """Linear classifiers with SGD training.

            Parameters
            ----------
            power_t : float, default=0.5
                The exponent for inverse scaling learning rate.

                .. deprecated:: 1.8
                    Negative values for `power_t` are deprecated in version 1.8 and will raise
                    an error in 1.10.
            """

            def __init__(self, power_t=0.5):
                self.power_t = power_t

            def score(self, X, y, sample_weight=None):
                """Return the score.

                Parameters
                ----------
                sample_weight : array-like of shape (n_samples,), default=None
                    Sample weights.

                    .. deprecated:: 1.9
                        `sample_weight` needs to be passed as a keyword argument and not as a
                        positional argument.
                """


        def yield_fixture(fixture_function=None, *args, scope="function"):
            """(Return a) decorator to mark a yield-fixture factory function.

            .. deprecated:: 3.0
                Use :py:func:`pytest.fixture` directly instead.
            """


        def and_(*clauses):
            """Produce a conjunction of expressions joined by ``AND``.

            .. deprecated:: 1.4  The ``and_()`` element now requires that at
               least one argument is passed; creating the ``and_()`` construct
               with no arguments is deprecated.
            """


        class Client:
            def embeddings(self, model="", prompt=""):
                """
                Deprecated in favor of `embed`.
                """

            def embed(self, model="", input=""):
                """Embed the input."""


        class ElasticKnnSearch:
            """[DEPRECATED] `Elasticsearch` with k-nearest neighbor search (`k-NN`) vector store."""


        class LanguageAuto:
            """Deprecated: Language auto-detection is now the default when language_codes is omitted."""


        class A2AConfig:
            """Configuration for A2A protocol integration.

            Deprecated:
                Use A2AClientConfig instead. This class will be removed in a future version.

            Attributes:
                endpoint: A2A agent endpoint URL.
            """


        def helpers():
            """Deprecation helpers for the package."""


        def names():
            """Return the deprecated names.

            Deprecated names are kept for one release.
            """
        '''
    ),
}


# What integration libraries reach into (DT-DATA-22): pymongo's `cursor._QUERY_OPTIONS`, instructor's
# `core.patch.handle_response_model` (an import), sqlmodel's `AsyncConnection` (imported from sqlalchemy).
INTERNALS = {
    "inner/__init__.py": (
        "from inner.core import Client\n"
        '__all__ = ["Client"]\n\n\n'
        "def __getattr__(name):\n"
        '    if name == "_old_stack":\n'
        "        import warnings\n\n"
        "        warnings.warn(\"'_old_stack' is deprecated and will be removed in 2.4.\", DeprecationWarning)\n"
        "        return 1\n"
        "    raise AttributeError(name)\n"
    ),
    "inner/core.py": (
        "from __future__ import annotations\n\n"
        "import os\n"
        "from typing import TYPE_CHECKING, Any\n"
        "from collections import OrderedDict as OD\n\n"
        "from markupsafe import escape\n\n"
        "from inner.response import Thing, handle_response\n"
        "from inner._impl import _helper\n\n"
        "if TYPE_CHECKING:\n"
        "    from inner.response import Hidden\n\n"
        '_QUERY_OPTIONS = {"tailable": 2}\n'
        "_annotated: int\n"
        "__dunder__ = 1\n"
        "_scratch = 1\n"
        "del _scratch\n\n\n"
        "def _check_name(name):\n"
        "    return name\n\n\n"
        "class _Wrapper:\n"
        "    def get(self):\n"
        "        pass\n\n\n"
        "class Client:\n"
        "    def _private(self):\n"
        "        pass\n"
    ),
    "inner/response.py": "def handle_response():\n    pass\n\n\nclass Thing:\n    pass\n\n\nclass Hidden:\n    pass\n",
    "inner/_impl.py": "import json\n\nfrom inner.response import Thing\n\n\ndef _helper():\n    pass\n\n\ndef visible():\n    pass\n",
    "inner/tests/__init__.py": "",
    "inner/tests/test_core.py": "def _fixture():\n    pass\n",
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


def test_names_all_leaves_out_are_public_when_defined_or_star_imported(tmp_path):
    # `__all__` names what `from m import *` binds. A module's own definitions are public anyway, and so are the
    # public names a star import brings from inside the package.
    snap = build_snapshot("listed", "1.0", ["listed"], _write(tmp_path, UNLISTED))
    found = {p: (r.kind, r.canonical) for p, r in snap.symbols.items()}
    assert found["listed.api.is_float_dtype"] == ("function", "listed.core.dtypes.is_float_dtype")
    assert found["listed.api.made_here"] == ("function", "listed.core.plain.made_here")
    assert found["listed.api.CategoricalDtype"] == ("class", "listed.core.dtypes.CategoricalDtype")
    assert found["listed.indexes.OpClass"] == ("class", "listed.indexes.OpClass")
    assert [p["name"] for p in snap.symbols["listed.indexes.OpClass.__init__"].params] == [
        "expression",
        "name",
    ]
    assert found["listed.indexes.api_key"] == ("attribute", "listed.indexes.api_key")
    assert found["listed.indexes.BTreeIndex"] == ("class", "listed.indexes.BTreeIndex")
    # Out: what the star source's `__all__` leaves out, a star source's own imports, imports by name, names bound
    # only for type checkers, annotations without a value, names the module deletes, and private names.
    assert not {
        "listed.api.is_hidden",
        "listed.api.Any",
        "listed.indexes.is_float_dtype",
        "listed.indexes.TYPE_CHECKING",
        "listed.indexes.Hint",
        "listed.indexes.timeout",
        "listed.indexes.v",
        "listed.indexes._private",
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
    # The branch returns `getattr(message, name)`: the name is that module's class, with the warning (DT-DATA-32).
    header = snap.symbols["tabled.mail.BadHeaderError"]
    assert (header.kind, header.canonical, header.deprecated) == (
        "class",
        "tabled.mail.message.BadHeaderError",
        "BadHeaderError is deprecated. Replace with ValueError.",
    )
    assert snap.symbols["tabled.mail.SafeMIMEText"].deprecated == "SafeMIMEText is deprecated."
    # Its members: django 6.0's `SafeMIMEText.as_string` read removed in 6.0.
    assert "tabled.mail.SafeMIMEText.as_string" in snap.symbols
    own = build_snapshot("tabled", "1.0", ["tabled"], _write(tmp_path / "own", TABLES), own_members=True)
    assert (
        own.expanded()["tabled.mail.SafeMIMEText.as_string"].canonical
        == "tabled.mail.message.SafeMIMEText.as_string"
    )
    assert snap.symbols["tabled.mail.EmailMessage"].kind == "class"
    thing = snap.symbols["tabled.lazy.Thing"]
    assert (thing.kind, thing.deprecated) == ("getattr", None)


def test_a_getattr_that_defines_its_own_table_serves_its_keys(tmp_path):
    # DT-DATA-43: starlette 0.48's old status-code names and pandas 3.1's `api.typing.RollingGroupby` read removed.
    snap = build_snapshot("localt", "1.0", ["localt"], _write(tmp_path, LOCAL_TABLES))
    old = snap.symbols["localt.status.HTTP_413_REQUEST_ENTITY_TOO_LARGE"]
    assert (old.kind, old.canonical) == ("getattr", "localt.status.__getattr__")
    assert (
        old.deprecated
        == "'HTTP_413_REQUEST_ENTITY_TOO_LARGE' is deprecated. Use 'HTTP_413_CONTENT_TOO_LARGE' instead."
    )
    # `if deprecated:` holds only for a true value; a `None` value is no value at all.
    assert not {"localt.status.HTTP_000_ZERO", "localt.status.HTTP_NONE"} & set(snap.symbols)
    # A name bound only for type checkers reaches `__getattr__` at runtime; `__name__` of a class is its own name.
    rolling = snap.symbols["localt.typing.RollingGroupby"]
    assert (rolling.kind, rolling.deprecated) == (
        "getattr",
        "localt.typing.RollingGroupby is deprecated. Use localt.typing.RollingGroupBy instead.",
    )
    # One `__all__` lists keeps the walker's record of it.
    listed = snap.symbols["localt.typing.Listed"]
    assert (listed.kind, listed.canonical, listed.deprecated) == (
        "class",
        "localt.window.RollingGroupBy",
        None,
    )
    # A name griffe can't find leaves its `__name__` unknown.
    assert snap.symbols["localt.typing.Elsewhere"].deprecated == (
        "localt.typing.Elsewhere is deprecated. Use localt.typing.… instead."
    )
    # `(new := table.get(name)) is not None` binds `new` for the warning; a key mapped to None isn't served.
    assert snap.symbols["localt.rename.old_name"].deprecated == "old_name is deprecated, use new_name instead"
    assert "localt.rename.gone" not in snap.symbols
    # The function's own table shadows the module's.
    assert snap.symbols["localt.moved.Mover"].deprecated == "Mover has moved to localt.window.RollingGroupBy"
    assert "localt.moved.Shadowed" not in snap.symbols


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


REBOUND = {
    "auto/__init__.py": "from auto.models import AutoModel, Legacy\n__all__ = ['AutoModel', 'Legacy']\n",
    "auto/models.py": (
        "def deprecated(message):\n    return lambda cls: cls\n\n"
        "def auto_class_update(cls, head_doc=''):\n    return cls\n\n"
        "class _BaseAuto:\n    def from_pretrained(cls, name):\n        pass\n\n"
        "class AutoModel(_BaseAuto):\n    pass\n\n"
        "AutoModel = auto_class_update(AutoModel, head_doc='models')\n\n"
        "class Legacy:\n    def run(self):\n        pass\n\n"
        "Legacy = deprecated('Legacy is deprecated, use AutoModel instead')(Legacy)\n\n"
        "def adds(n):\n    return lambda fn: fn\n\n"
        "def CALLBACK(fn):\n    return fn\n\n"
        "def notice(fmt, lst):\n    pass\n\n"
        "notice = CALLBACK(notice)\n\n"
        "def old(x):\n    pass\n\n"
        "old = deprecated('old is deprecated, use notice instead')(old)\n\n"
        "class Filter:\n"
        "    def args_check(name, func, provided):\n        pass\n"
        "    args_check = staticmethod(args_check)\n\n"
        "    def as_manager(cls, extra=None):\n        pass\n"
        "    as_manager = classmethod(as_manager)\n\n"
        "    def append(self, item):\n        pass\n"
        "    append = adds(1)(append)\n\n"
        "    def session(self):\n        pass\n"
        "    session = property(session, lambda s, x: None)\n"
    ),
}


def test_a_class_rebound_to_a_call_on_itself_is_still_the_class(tmp_path):
    # DT-DATA-50: transformers' `AutoModel = auto_class_update(AutoModel)` read as an attribute without members.
    for own in (False, True):
        snap = build_snapshot("auto", "1.0", ["auto"], _write(tmp_path / str(own), REBOUND), own_members=own)
        symbols = snap.expanded()
        assert symbols["auto.AutoModel"].kind == "class"
        assert symbols["auto.AutoModel.from_pretrained"].canonical == "auto.models._BaseAuto.from_pretrained"
        # A deprecation wrapper stays the attribute its message is read from.
        assert symbols["auto.Legacy"].kind == "attribute"
        assert "deprecated" in symbols["auto.Legacy"].deprecated


# numpy 2.2+: `numpy._core.multiarray` star-imports a compiled module, and its stub imports `ndarray` from `numpy`,
# whose stub defines it. griffe reports an alias cycle for every import of it under `numpy._core`.
CYCLE = {
    "cyc/__init__.py": "from cyc._core import ndarray\nfrom cyc import core\n__all__ = ['ndarray', 'core']\n",
    "cyc/__init__.pyi": "class ndarray:\n    def reshape(self, shape): ...\n    def view(self): ...\n",
    "cyc/_core/__init__.py": "from .numeric import *\nfrom . import multiarray\n",
    "cyc/_core/numeric.py": "from .multiarray import ndarray\n__all__ = ['ndarray']\n",
    "cyc/_core/multiarray.py": "from cyc._core._umath import *\n__all__ = ['ndarray']\n",
    "cyc/_core/multiarray.pyi": "from cyc import ndarray\n",
    "cyc/_core/loop.py": "from cyc._core.loop2 import thing\n",
    "cyc/_core/loop2.py": "from cyc._core.loop import thing\n",
    "cyc/core/__init__.py": "",
    "cyc/core/multiarray.py": (
        "def __getattr__(name):\n"
        "    import warnings\n"
        "    from cyc._core import multiarray\n"
        "    warnings.warn('cyc.core is deprecated', DeprecationWarning)\n"
        "    return getattr(multiarray, name)\n"
    ),
    "cyc/core/loop.py": (
        "def __getattr__(name):\n    from cyc._core import loop\n    return getattr(loop, name)\n"
    ),
}


def test_an_import_griffe_calls_cyclic_is_followed_by_hand(tmp_path):
    # DT-DATA-32: from numpy 2.2 the `numpy.core` forwarders served `ndarray`, `dtype`, `nditer` and `broadcast` as
    # unresolved imports, and as classes before and after.
    for own in (False, True):
        snap = build_snapshot("cyc", "1.0", ["cyc"], _write(tmp_path / str(own), CYCLE), own_members=own)
        symbols = snap.expanded()
        forwarded = symbols["cyc.core.multiarray.ndarray"]
        assert (forwarded.kind, forwarded.canonical, forwarded.deprecated) == (
            "class",
            "cyc.ndarray",
            "cyc.core is deprecated",
        )
        # With own members, a forwarded class has the members of the class it is.
        assert ("cyc.core.multiarray.ndarray.reshape" in symbols) is own
        # A real cycle stays an unresolved import.
        assert symbols["cyc.core.loop.thing"].kind == "alias"


# instructor 1.17's `instructor.v2`: lazy names listed next to the keys of a dict another module computes.
STARRED_ALL = {
    "ins/__init__.py": "",
    "ins/v2/core/__init__.py": "",
    "ins/v2/core/specs.py": "TABLE = dict((n, ('ins.v2.core.mode', n)) for n in ['from_x'])\n",
    "ins/v2/core/mode.py": "class Mode:\n    pass\n",
    "ins/v2/__init__.py": (
        "from importlib import import_module\n"
        "from ins.v2.core.specs import TABLE\n\n"
        "__all__ = [\n    'Mode',\n    'core',\n    *TABLE.keys(),\n]\n\n"
        "_LAZY = {'Mode': ('ins.v2.core.mode', 'Mode'), 'core': ('ins.v2.core', None), **TABLE}\n\n\n"
        "def __getattr__(name):\n"
        "    if name not in __all__:\n"
        "        raise AttributeError(name)\n"
        "    module_path, attr_name = _LAZY[name]\n"
        "    module = import_module(module_path)\n"
        "    return module if attr_name is None else getattr(module, attr_name)\n"
    ),
}


def test_an_all_with_a_starred_element_keeps_its_literal_names(tmp_path):
    # DT-DATA-28: griffe reads no `__all__` from one it can't evaluate, so instructor 1.17's `instructor.v2.Mode`,
    # `Provider` and `patch_v2` were missing, and its imports and `__all__` read as public.
    snap = build_snapshot("ins", "1.0", ["ins"], _write(tmp_path, STARRED_ALL))
    top = {p: r.kind for p, r in snap.symbols.items() if p.startswith("ins.v2.") and p.count(".") == 2}
    assert top == {"ins.v2.Mode": "getattr", "ins.v2.core": "module"}
    assert "ins.v2.TABLE" in snap.internal


def test_following_an_import_by_hand_gives_up_on_a_path_griffe_cant_follow(tmp_path):
    # chromadb 0.5.7: the path runs through another distribution's module, and griffe raised from inside the walk.
    from deptime.ingest.snapshot import _through_cycle

    files = {"thru/__init__.py": "from elsewhere import ext\nfrom thru.ext.sub import thing\n"}
    loaded = griffe.load("thru", search_paths=[str(_write(tmp_path, files))], allow_inspection=False)
    assert _through_cycle(loaded.members["thing"]) is None


def test_a_function_rebound_to_a_call_on_itself_is_still_the_function(tmp_path):
    # DT-DATA-50: django's `args_check = staticmethod(args_check)` and `as_manager = classmethod(as_manager)` read as
    # attributes without a signature, and so did django.contrib.gis's ctypes callbacks (`notice_h = NOTICEFUNC(...)`).
    files = REBOUND | {"auto/__init__.py": "from auto.models import Filter, notice, old\n"}
    for own in (False, True):
        snap = build_snapshot("auto", "1.0", ["auto"], _write(tmp_path / str(own), files), own_members=own)
        symbols = snap.expanded()
        for path, params in (
            ("auto.Filter.args_check", ["name", "func", "provided"]),
            ("auto.Filter.as_manager", ["extra"]),
            ("auto.Filter.append", ["item"]),
            ("auto.notice", ["fmt", "lst"]),
        ):
            assert symbols[path].kind == "function", path
            assert [p["name"] for p in symbols[path].params] == params
        # `property(f)` is an attribute, as griffe has `@property`; a deprecation wrapper keeps its message there.
        assert symbols["auto.Filter.session"].kind == "attribute"
        assert symbols["auto.old"].kind == "attribute"
        assert "deprecated" in symbols["auto.old"].deprecated


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
    made = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("__init__.py") if not p.read_text()
    )
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


def test_a_function_takes_the_deprecation_its_dispatcher_warns(tmp_path):
    # DT-DATA-40: numpy 1.25's `product` forwards to `prod`; the warning is in `_product_dispatcher`, which
    # `@array_function_dispatch(...)` is given. The index recorded no deprecation for `product`.
    snap = build_snapshot("dispy", "1.25.0", ["dispy"], _write(tmp_path, DISPATCHED))

    def dep(path):
        rec = snap.symbols[f"dispy.{path}"]
        return rec.deprecated, rec.deprecated_params

    assert dep("product") == (
        "`product` is deprecated as of NumPy 1.25.0, and will be removed in NumPy 2.0. Please use `prod` instead.",
        None,
    )
    assert dep("prod") == (None, None)
    # A dispatcher warning that doesn't name the function, or a function passed for its docstring whose warning
    # deprecates one of its arguments or names itself, isn't the decorated function's deprecation.
    assert dep("sum") == (None, None)
    # `mask` passes `try_cast` on to `where`, which deprecates it (DT-DATA-39); `mask` itself isn't deprecated.
    assert dep("mask") == (None, {"try_cast": "try_cast keyword is deprecated and will be removed."})
    assert dep("select") == (None, None)
    assert dep("lookup") == ("lookup() is deprecated, use select() instead.", None)


def test_an_argument_passed_on_to_a_function_that_deprecates_it_is_deprecated(tmp_path):
    # DT-DATA-39: warnings one call away. An argument a function passes on under its own name, to a function or
    # class of its package (by name, `self.`, `super()`, a module's attribute), takes that function's deprecation
    # of it, up to three calls down.
    snap = build_snapshot("passy", "1.0", ["passy"], _write(tmp_path, PASSED))

    def dep(path):
        rec = snap.symbols[f"passy.{path}"]
        return rec.deprecated, rec.deprecated_params

    proxies = "The `proxies` argument is deprecated. The `http_client` argument should be passed instead"
    cert = (
        "`cert=...` is deprecated. Use `verify=<ssl_context>` instead,with `.load_cert_chain()` to configure the "
        "certificate chain."
    )
    # A warning about some values (`timeout`, `verify`) isn't the argument's, and neither is one whose wording is
    # about a value or a way of calling, though the base class's own reading takes `filter` and `issuer` from it;
    # `client_cert` is passed on as `cert`.
    assert dep("Client.__init__") == (None, {"proxies": proxies, "cert": cert})
    assert dep("Client.ping") == (None, None)
    regex = "`regex` has been deprecated, please use `pattern` instead"
    assert dep("params.Query.__init__") == (None, {"regex": regex})
    assert dep("Query") == (None, {"regex": regex})
    # A literal the caller passes fills in the helper's message; a constant from another module is read.
    assert dep("skip") == (
        None,
        {"msg": "pytest.skip(msg=...) is now deprecated, use pytest.skip(reason=...) instead"},
    )
    assert dep("fail")[1] == {
        "msg": "pytest.fail(msg=...) is now deprecated, use pytest.fail(reason=...) instead"
    }
    assert dep("exit") == (
        None,
        {"msg": "pytest.exit(msg=...) is now deprecated, use pytest.exit(reason=...) instead"},
    )
    assert dep("yield_fixture") == (
        "@pytest.yield_fixture is deprecated. Use @pytest.fixture instead; they are the same.",
        None,
    )
    # Passed positionally under the same name (pandas' `mask` -> `where`).
    assert dep("mask") == (None, {"try_cast": "try_cast keyword is deprecated and will be removed."})
    # A docstring note on the argument in the function called counts, whatever its wording.
    assert dep("resample") == (
        None,
        {
            "base": "Deprecated since version 1.1.0: The new arguments that you should use are 'offset' or 'origin'."
        },
    )
    # Passed on under another name, it isn't followed.
    assert dep("Mounts.__init__") == (None, None)
    # Through **kwargs passed on whole, unless the function looks the name up itself or rebinds them.
    missing = "The 'missing' argument to fields is deprecated. Use 'load_default' instead."
    assert dep("Field.__init__") == (None, {"missing": missing})
    assert dep("IP.__init__") == (None, {"missing": missing})
    assert dep("Strict.__init__") == (None, None)
    assert dep("Filtered.__init__") == (None, None)
    # The function's own docstring note says it first.
    assert dep("where") == (
        None,
        {"try_cast": "Deprecated since version 1.3.0: Manually cast back if necessary."},
    )
    # Four calls down is too far, and a cycle ends.
    assert dep("timed") == (None, None)
    # A warning about a value (`not isinstance(...)`) isn't the argument's, though its URL says "write-line".
    assert dep("write_line") == (None, None)
    # Only for some value of another argument that the caller doesn't pass on as its own (pandas' `obj=self`).
    assert dep("Frame.describe") == (None, None)
    # An argument the warning names beside the one passed is a co-subject, not a condition (starlette).
    startup = (
        "The on_startup and on_shutdown parameters are deprecated, and they will be removed on version 1.0. Use "
        "the lifespan parameter instead."
    )
    assert dep("App.__init__") == (None, {"on_startup": startup, "on_shutdown": startup})
    assert dep("StartupOnly.__init__") == (None, {"on_startup": startup})


def test_deprecation_helpers_are_recognized_by_name(tmp_path):
    # DT-DATA-37: only decorators named `deprecated` counted. polars' `@deprecate_renamed_function("map_elements")`
    # left `Expr.apply` with nothing, streamlit's `experimental_rerun = _deprecate_func_name(_rerun, ...)` read as a
    # plain attribute, and huggingface-hub's `cached_download` lost its `hf_hub_download` hint with DT-DATA-42.
    snap = build_snapshot("helpy", "1.0", ["helpy"], _write(tmp_path, HELPERS))

    def dep(path):
        rec = snap.symbols[f"helpy.{path}"]
        return rec.deprecated, rec.deprecated_params

    # The decorated function or class as a whole: a new name becomes "Use `x` instead.", prose is kept, and
    # sqlalchemy's reST roles read as its warnings print them.
    assert dep("api.Expr.apply") == ("apply is deprecated. Use `map_elements` instead.", None)
    assert dep("api.Expr.map_alias") == ("map_alias is deprecated. Use `name.map` instead.", None)
    assert dep("api.Expr.set_ordering") == (
        "set_ordering is deprecated. Set the ordering on the data type instead.",
        None,
    )
    assert dep("api.Redis.close") == ("close is deprecated. Use aclose() instead", None)
    assert dep("api.cached_download") == (
        "cached_download is deprecated. Use `hf_hub_download` instead.",
        None,
    )
    # A constructor's deprecation is its class's.
    assert dep("api.InferenceApi.__init__") == (
        "InferenceApi is deprecated. Use `InferenceClient` instead.",
        None,
    )
    assert dep("api.Query.get") == ("get is deprecated. The method is now available as Session.get()", None)
    assert dep("api.relation") == ("relation is deprecated. Please use relationship().", None)
    assert dep("api.MapperOption") == (
        "The MapperOption class is deprecated and will be removed in a future release.",
        None,
    )
    # `typing_extensions.deprecated` imported under another name is still `@deprecated`.
    assert dep("api.connect_to_wcs") == (
        "This method is deprecated. Use `connect_to_weaviate_cloud` instead.",
        None,
    )
    # Arguments: renamed (the old name need not be in the signature), deleted, or listed.
    assert dep("api.Expr.shift") == (None, {"periods": "`periods` is deprecated. Use `n` instead."})
    assert dep("api.Redis.__init__") == (
        None,
        {"retry_on_timeout": "`retry_on_timeout` is deprecated. TimeoutError is included by default."},
    )
    assert dep("api.list_datasets") == (None, {"cardData": "`cardData` is deprecated. Use 'full' instead."})
    assert dep("api.Colorbar.grid") == (None, {"b": "`b` is deprecated. Use `visible` instead."})
    assert dep("api.Colorbar.get_tightbbox") == (None, {"renderer": "`renderer` is deprecated."})
    assert dep("api.MetaData.__init__") == (
        None,
        {"bind": "The MetaData.bind argument is deprecated and will be removed in SQLAlchemy 2.0."},
    )
    assert dep("api.create_react_agent") == (
        None,
        {"messages_modifier": "`messages_modifier` is deprecated. Use `state_modifier` instead."},
    )
    assert dep("api.to_excel") == (None, {"verbose": "`verbose` is deprecated."})
    assert dep("api.reindex_like") == (None, {"method": "`method` is deprecated."})
    # Positional use isn't a deprecation of the function or of an argument.
    assert dep("api.Expr.sort") == (None, None)
    # Warning helpers called in a body read like `warn(...)`.
    assert dep("api.Expr.clip") == (None, {"min_val": "`min_val` is deprecated, use `lower` instead."})
    assert dep("api.Colorbar.set_label") == (
        None,
        {"beginarrow": "`beginarrow` is deprecated. Use `arrow` instead."},
    )
    assert dep("api.Colorbar.resize") == (
        "`resize` is deprecated. Use `FigureManagerBase.resize` instead.",
        None,
    )
    # Under a condition that isn't about an argument, a helper deprecates a behaviour; a subject that isn't a name
    # is something else. Neither is the whole function, but an argument that guards it is still deprecated.
    assert dep("api.Colorbar.clabel") == (None, None)
    assert dep("api.Colorbar.bxp") == (
        None,
        {"vert": "`vert: bool` is deprecated. orientation: {'vertical', 'horizontal'}"},
    )
    assert dep("api.image") == (
        None,
        {
            "use_column_width": "The `use_column_width` parameter has been deprecated and will be removed in a future"
            " release. Please utilize the `use_container_width` parameter instead."
        },
    )
    assert dep("api.dataframe") == (
        None,
        {
            "use_container_width": "`use_container_width` is deprecated. Use `width` instead. For"
            " `use_container_width=True`, use `width='stretch'`."
        },
    )
    # A wrapper bound to the old name: the wrapped function's own name (or the override) is the new one, at the top
    # of the package (streamlit's `st.rerun`).
    assert snap.symbols["helpy.experimental_rerun"].kind == "attribute"
    assert dep("experimental_rerun") == ("experimental_rerun is deprecated. Use `helpy.rerun` instead.", None)
    assert dep("experimental_get_query_params") == (
        "experimental_get_query_params is deprecated. Use `helpy.query_params` instead. Refer to the docs page"
        " for more information.",
        None,
    )
    # streamlit 1.27-1.36: a private function warns about the name it is served under.
    assert dep("older_rerun") == ("`experimental_rerun` is deprecated. Use `helpy.rerun` instead.", None)
    assert dep("io.json_normalize") == (
        "json_normalize is deprecated. Use `helpy.json_normalize` instead.",
        None,
    )
    assert dep("api.Colorbar.patch") == ("patch is deprecated. Use `ax` instead.", None)
    assert dep("api.Colorbar.ax") == (None, None)
    # The machinery warns as its job; that isn't its own deprecation.
    assert dep("utils.warn_deprecated") == (None, None)
    assert dep("utils.issue_deprecation_warning") == (None, None)


DECORATORS = {
    "decky/__init__.py": (
        "import warnings\n"
        "from typing_extensions import deprecated\n"
        "from celery.exceptions import CPendingDeprecationWarning\n"
        "from langchain_core._api import deprecated as lc_deprecated\n"
        "from matplotlib import _api\n"
        "from sqlalchemy import util\n"
        "from langgraph._api.deprecation import deprecated as lg_deprecated\n"
        "from deprecation import deprecated as deprecation_deprecated\n\n"
        "MESSAGE = 'Use `NewModel` instead.'\n\n\n"
        "class Chain:\n"
        "    @lc_deprecated('0.1.0', alternative='invoke', removal='0.2.0')\n"
        "    def run(self): ...\n\n"
        "    @lc_deprecated(since='0.2.13', removal='1.0', message='Refer here for a map-reduce implementation.')\n"
        "    def map_reduce(self): ...\n\n"
        "    @lc_deprecated('0.0.260', alternative='from_messages classmethod', pending=True)\n"
        "    def from_role_strings(self): ...\n\n"
        "    @lc_deprecated(since='0.0.21', alternative_import='from langchain_huggingface import HuggingFaceEndpoint')\n"
        "    def huggingface(self): ...\n\n\n"
        "class Model:\n"
        "    @lc_deprecated(since='0.1.23', removal='0.2.0', alternative_import='langchain_astradb.AstraDBVectorStore')\n"
        "    def __init__(self): ...\n\n\n"
        "@deprecated('Please use `numpy.fft.fftshift` instead.')\n"
        "def fftshift(x): ...\n\n\n"
        "@deprecated(MESSAGE, category=DeprecationWarning)\n"
        "def old_model(): ...\n\n\n"
        "@deprecated('`soon` will be deprecated.', category=PendingDeprecationWarning)\n"
        "def soon(): ...\n\n\n"
        "@_api.deprecated('3.6', pending=True, alternative='``matplotlib.colormaps.register(name)``')\n"
        "def register_cmap(name): ...\n\n\n"
        "@_api.deprecated('3.6', alternative=\"callbacks.process('draw_event', DrawEvent(...))\")\n"
        "def draw_event(): ...\n\n\n"
        "@_api.deprecated('3.6', alternative='cmap.with_extremes(bad=...) or Colormap(bad=...)')\n"
        "def set_bad(): ...\n\n\n"
        "@_api.deprecated('3.10.0', message='Never used internally.', alternative='No replacement.  Please open an issue.')\n"
        "def unused(): ...\n\n\n"
        "@_api.deprecated('2.0', name='wx', obj_type='backend', removal='the future', alternative='wxagg', addendum='See the FAQ.')\n"
        "def backend_wx(): ...\n\n\n"
        "@_api.deprecated('3.5')\n"
        "def since_only(): ...\n\n\n"
        "def _get_cmap(name=None): ...\n\n\n"
        "get_cmap = _api.deprecated('3.7', name='get_cmap', alternative='``matplotlib.colormaps[name]``')(_get_cmap)\n\n\n"
        "@util.deprecated('1.4', 'The :meth:`_engine.Connection.run_callable` method is deprecated.')\n"
        "def run_callable(): ...\n\n\n"
        "@lg_deprecated('0.0.44', 'create_react_agent', removal='0.2.0')\n"
        "def chat_agent_executor(): ...\n\n\n"
        "@deprecation_deprecated(deprecated_in='4.6.2', details='This method is deprecated and will be removed in a future release. Use :func:`connect_to_weaviate_cloud` instead.')\n"
        "def connect_to_wcs(): ...\n\n\n"
        "@deprecated\n"
        "def bare(): ...\n\n\n"
        "class matrix:\n"
        "    def tolist(self):\n"
        "        warnings.warn('the matrix subclass is not the recommended way', PendingDeprecationWarning)\n\n"
        "    def set_tight_layout(self, tight):\n"
        "        _api.warn_deprecated('3.6', alternative='set_layout_engine', pending=True)\n\n"
        "    def link_error(self, errback):\n"
        "        warnings.warn('link_error is pending deprecation', CPendingDeprecationWarning)\n\n"
        "    @_api.delete_parameter('3.6', 'renderer', pending=True)\n"
        "    def get_tightbbox(self, renderer=None): ...\n\n"
        "    def fill(self):\n"
        "        warnings.warn('fill is deprecated', DeprecationWarning)\n"
    ),
}


def test_deprecated_decorators_are_read_by_argument(tmp_path):
    # DT-DATA-31: the decorator's argument source was the message ("'0.1.0', alternative='invoke', removal='0.2.0'"),
    # for 4,726 of 10,836 deprecation events. Its arguments are now read: the message unquoted, when and until when,
    # the alternative as "Use … instead.", and a pending deprecation isn't one.
    snap = build_snapshot("decky", "1.0", ["decky"], _write(tmp_path, DECORATORS))

    def dep(path):
        return snap.symbols[f"decky.{path}"].deprecated

    assert (
        dep("Chain.run") == "run was deprecated in 0.1.0 and will be removed in 0.2.0. Use `invoke` instead."
    )
    assert dep("Chain.map_reduce") == (
        "map_reduce was deprecated in 0.2.13 and will be removed in 1.0. Refer here for a map-reduce implementation."
    )
    assert dep("Chain.huggingface") == (
        "huggingface was deprecated in 0.0.21. Use `from langchain_huggingface import HuggingFaceEndpoint` instead."
    )
    assert dep("Model.__init__") == (
        "Model was deprecated in 0.1.23 and will be removed in 0.2.0. Use `langchain_astradb.AstraDBVectorStore` instead."
    )
    # PEP 702: the message, unquoted; a module-level constant is read.
    assert dep("fftshift") == "Please use `numpy.fft.fftshift` instead."
    assert dep("old_model") == "Use `NewModel` instead."
    # matplotlib's alternatives are code, reST literals, or a sentence of their own; `name=` is the subject.
    assert dep("draw_event") == (
        "draw_event was deprecated in 3.6. Use `callbacks.process('draw_event', DrawEvent(...))` instead."
    )
    assert (
        dep("set_bad")
        == "set_bad was deprecated in 3.6. Use `cmap.with_extremes(bad=...)` or `Colormap(bad=...)` instead."
    )
    assert (
        dep("unused")
        == "unused was deprecated in 3.10.0. Never used internally. No replacement. Please open an issue."
    )
    assert dep("backend_wx") == (
        "wx was deprecated in 2.0 and will be removed in the future. Use `wxagg` instead. See the FAQ."
    )
    assert dep("since_only") == "since_only was deprecated in 3.5."
    # Applied by hand to bind a name (matplotlib 3.7's `cm.get_cmap`).
    assert dep("get_cmap") == "get_cmap was deprecated in 3.7. Use `matplotlib.colormaps[name]` instead."
    # sqlalchemy's (version, message), langgraph's (since, alternative), the deprecation library's keywords.
    assert dep("run_callable") == "The Connection.run_callable() method is deprecated."
    assert dep("chat_agent_executor") == (
        "chat_agent_executor was deprecated in 0.0.44 and will be removed in 0.2.0. Use `create_react_agent` instead."
    )
    # A message that says what is deprecated stands alone.
    assert dep("connect_to_wcs") == (
        "This method is deprecated and will be removed in a future release. Use connect_to_weaviate_cloud() instead."
    )
    assert dep("bare") == "deprecated"
    # Pending: not deprecated yet, whether a decorator, a helper or a warning in the body says so.
    assert dep("Chain.from_role_strings") is None
    assert dep("register_cmap") is None
    assert dep("soon") is None
    assert dep("matrix.tolist") is None
    assert dep("matrix.set_tight_layout") is None
    assert dep("matrix.link_error") is None
    assert snap.symbols["decky.matrix.get_tightbbox"].deprecated_params is None
    assert dep("matrix.fill") == "fill is deprecated"


def test_docstring_notes_and_deprecated_defaults_are_deprecations(tmp_path):
    # DT-DATA-38: scikit-learn 1.2-1.3's `OneHotEncoder(sparse='deprecated')` warns only in `fit`, and the
    # replacement (`sparse_output`) is only in the class docstring's `.. deprecated::` note.
    snap = build_snapshot("docky", "1.0", ["docky"], _write(tmp_path, DOCNOTES))

    def dep(path):
        rec = snap.symbols[f"docky.{path}"]
        return rec.deprecated, rec.deprecated_params

    # A constructor's parameters are documented in the class docstring. A note that deprecates one of the
    # parameter's values, or says it will be deprecated, isn't the parameter's; a `'deprecated'` default is.
    assert dep("OneHotEncoder") == (None, None)
    assert dep("OneHotEncoder.__init__") == (
        None,
        {
            "sparse": "Deprecated since version 1.2: `sparse` is deprecated in 1.2 and will be removed in 1.4. Use"
            " `sparse_output` instead.",
            "random_state": "Deprecated since version 1.9: The `random_state` parameter is deprecated and will be"
            " removed in 1.11.",
            "base_estimator": "deprecated (marked by its default value)",
        },
    )
    # A note at the top of a docstring deprecates the object; reST roles and literals read as plain text.
    assert dep("CodeAct") == (
        "Deprecated since version 3.4: CodeAct is deprecated. RLM is the preferred replacement.",
        None,
    )
    assert dep("CodeAct.__init__") == (None, None)
    assert dep("find_idx_by_name") == (
        "Deprecated since version 0.19.14: This method has been renamed to get_column_index().",
        None,
    )
    # A version alone deprecates the parameter it's under; `**kwargs` is never a deprecated parameter.
    assert dep("percentile") == (None, {"interpolation": "Deprecated since version 1.22.0."})
    # A deprecated behaviour isn't a deprecated function, and a pending deprecation isn't one yet.
    assert dep("to_pydatetime") == (None, None)
    assert dep("future_axis") == (None, None)
    # A top-level note whose wording deprecates a parameter deprecates that parameter.
    assert dep("Enum.__init__") == (
        None,
        {
            "inherit_schema": "Deprecated since version 2.1: Setting the Enum.inherit_schema parameter is deprecated."
        },
    )
    assert dep("connect") == (None, {"timeout": "Deprecated since version 2.0: Use `retries` instead."})
    # The warning the code emits wins over the docstring, unless the snapshot can't read it.
    assert dep("warned") == (None, {"old": "'old' is deprecated, use 'new' instead"})
    assert dep("unreadable") == ("Deprecated since version 1.5: Use `readable` instead.", None)
    # sqlalchemy documents parameters as Sphinx fields; a note inside another directive (`.. note::`) isn't the
    # object's. An entry can name an argument `**kwargs` takes.
    assert dep("Session.flush") == (None, {"objects": "Deprecated since version 2.1."})
    assert dep("Session.distinct") == (None, None)
    assert dep("create_engine") == (
        None,
        {
            "convert_unicode": "Deprecated since version 1.3: The create_engine.convert_unicode parameter is"
            " deprecated and will be removed in a future release."
        },
    )
    # What is deprecated is one of the parameter's values, or a way of passing it; numpy says so in the entry's
    # own text above a bare version.
    assert dep("binary_repr") == (None, None)
    assert dep("to_excel") == (None, None)
    assert dep("SGDClassifier.__init__") == (None, None)
    assert dep("SGDClassifier.score") == (None, None)
    assert dep("yield_fixture") == (
        "Deprecated since version 3.0: Use pytest.fixture() directly instead.",
        None,
    )
    # A note at the top about the arguments or a usage isn't about the whole object.
    assert dep("and_") == (None, None)
    # A docstring that opens with "Deprecated", or has a google-style `Deprecated:` section, deprecates the object
    # whatever the rest says.
    assert dep("Client.embeddings") == ("Deprecated in favor of `embed`.", None)
    assert dep("Client.embed") == (None, None)
    assert dep("ElasticKnnSearch") == (
        "[DEPRECATED] `Elasticsearch` with k-nearest neighbor search (`k-NN`) vector store.",
        None,
    )
    assert dep("LanguageAuto") == (
        "Deprecated: Language auto-detection is now the default when language_codes is omitted.",
        None,
    )
    assert dep("A2AConfig") == (
        "Deprecated: Use A2AClientConfig instead. This class will be removed in a future version.",
        None,
    )
    assert dep("helpers") == (None, None)
    assert dep("names") == (None, None)


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


def test_internal_names_are_recorded_apart_from_the_public_api(tmp_path):
    # DT-DATA-22: names a module binds that aren't public API, kept apart so "never existed" stays true of them.
    snap = build_snapshot("inner", "1.0", ["inner"], _write(tmp_path, INTERNALS))
    internal = {path: (rec.kind, rec.canonical) for path, rec in snap.internal.items()}
    assert internal == {
        "inner.core._QUERY_OPTIONS": ("attribute", "inner.core._QUERY_OPTIONS"),
        "inner.core._check_name": ("function", "inner.core._check_name"),
        "inner.core._Wrapper": ("class", "inner.core._Wrapper"),
        # Imports a module makes for its own use, from inside the package or another distribution.
        "inner.core.Thing": ("class", "inner.response.Thing"),
        "inner.core.handle_response": ("function", "inner.response.handle_response"),
        "inner.core._helper": ("function", "inner._impl._helper"),
        "inner.core.escape": ("alias", "markupsafe.escape"),
        # A private module, and everything it binds.
        "inner._impl": ("module", "inner._impl"),
        "inner._impl._helper": ("function", "inner._impl._helper"),
        "inner._impl.visible": ("function", "inner._impl.visible"),
        "inner._impl.Thing": ("class", "inner.response.Thing"),
        # A private name a module __getattr__ serves.
        "inner._old_stack": ("getattr", "inner.__getattr__"),
    }
    assert (
        snap.internal["inner._old_stack"].deprecated
        == "'_old_stack' is deprecated and will be removed in 2.4."
    )
    # Public paths stay where they were, and nothing is in both.
    assert {"inner.Client", "inner.core.Client", "inner.response.handle_response"} <= set(snap.symbols)
    assert not set(snap.internal) & set(snap.symbols)
    # Not recorded: standard-library imports, names only type checkers see, annotations without a value, dunders,
    # deleted names, class members, and test modules.
    assert not [p for p in snap.symbols if "_QUERY_OPTIONS" in p or "_impl" in p]


def test_a_getattr_serves_private_names_as_internal(tmp_path):
    snap = build_snapshot("shimmy", "1.0", ["shimmy"], _write(tmp_path, SHIMS))
    assert (snap.internal["shimmy._private"].kind, snap.internal["shimmy._private"].deprecated) == (
        "getattr",
        None,
    )
    assert "shimmy._private" not in snap.symbols


def test_snapshot_roundtrip_keeps_internal_names(tmp_path):
    snap = build_snapshot("inner", "1.0", ["inner"], _write(tmp_path / "src", INTERNALS))
    out = tmp_path / "s.json.gz"
    snap.save(out)
    again = ApiSnapshot.load(out)
    assert again.internal == snap.internal and again.symbols == snap.symbols


_LAZY_INIT = (
    "from typing import TYPE_CHECKING\n"
    "from ...utils import _LazyModule\n"
    "from ...utils.import_utils import define_import_structure\n\n"
    "if TYPE_CHECKING:\n"
    "    from .{star} import *\n"
    "else:\n"
    "    import sys\n\n"
    '    _file = globals()["__file__"]\n'
    "    sys.modules[__name__] = _LazyModule(__name__, _file, define_import_structure(_file), module_spec=__spec__)\n"
)

# transformers 4.57: `_LazyModule(..., define_import_structure(_file))` exports what the folder's modules list, and
# the generated `TYPE_CHECKING` imports can be stale (t5gemma's names `configuration_encdecgemma2`).
LAZY_STRUCTURE = {
    "tfm/__init__.py": (
        "import sys\n"
        "from pathlib import Path\n"
        "from typing import TYPE_CHECKING\n"
        "from .utils import _LazyModule\n"
        "from .utils.import_utils import define_import_structure\n\n"
        "if TYPE_CHECKING:\n"
        "    from .configuration_utils import PretrainedConfig\n"
        "    from .models import *\n"
        "else:\n"
        '    import_structure = define_import_structure(Path(__file__).parent / "models", prefix="models")\n'
        '    sys.modules[__name__] = _LazyModule(__name__, globals()["__file__"], import_structure)\n'
    ),
    "tfm/configuration_utils.py": "class PretrainedConfig:\n    pass\n",
    "tfm/utils/__init__.py": "class _LazyModule:\n    pass\n",
    "tfm/utils/import_utils.py": (
        "def define_import_structure(path, prefix=None):\n    return {}\n\n\n"
        "def requires(*, backends=()):\n    return lambda obj: obj\n"
    ),
    "tfm/models/__init__.py": _LAZY_INIT.replace("...", "..").format(star="bert"),
    "tfm/models/bert/__init__.py": _LAZY_INIT.format(star="configuration_bert"),
    "tfm/models/bert/configuration_bert.py": 'class BertConfig:\n    pass\n\n\n__all__ = ["BertConfig"]\n',
    "tfm/models/bert/modeling_bert.py": (
        'class BertLayer:\n    pass\n\n\nclass BertModel:\n    pass\n\n\n__all__ = ["BertModel"]\n'
    ),
    "tfm/models/gemma/__init__.py": _LAZY_INIT.format(star="configuration_oldgemma"),
    "tfm/models/gemma/configuration_gemma.py": (
        'class GemmaConfig:\n    pass\n\n\n__all__ = ["Old"]\n__all__ = ["GemmaConfig"]\n'
    ),
    "tfm/models/gemma/modeling_gemma.py": (
        "from ...utils.import_utils import requires\n\n\n"
        '@requires(backends=("torch",))\nclass GemmaModel:\n    pass\n\n\nclass GemmaHelper:\n    pass\n'
    ),
    "tfm/models/gemma/modular_gemma.py": 'class ModularOnly:\n    pass\n\n\n__all__ = ["ModularOnly"]\n',
    "tfm/models/gemma/convert_gemma.py": "def main():\n    pass\n",
    "tfm/plain/__init__.py": "from tfm.utils.import_utils import define_import_structure\n",
    "tfm/plain/things.py": 'class Thing:\n    pass\n\n\n__all__ = ["Thing"]\n',
}


def test_a_lazy_package_exports_what_its_folder_lists(tmp_path):
    # DT-DATA-05: transformers 4.57's `models/__init__.py` leaves out 15 models and t5gemma's `__init__` star-imports
    # a module that doesn't exist, so their names read absent though `_LazyModule` serves them.
    for own in (False, True):
        snap = build_snapshot(
            "tfm", "1.0", ["tfm"], _write(tmp_path / str(own), LAZY_STRUCTURE), own_members=own
        )
        symbols = snap.expanded()
        for path in ("tfm.models.gemma", "tfm.models", "tfm"):
            assert (
                symbols[f"{path}.GemmaConfig"].canonical == "tfm.models.gemma.configuration_gemma.GemmaConfig"
            )
            assert symbols[f"{path}.GemmaModel"].canonical == "tfm.models.gemma.modeling_gemma.GemmaModel"
            bert = "tfm.models.bert" if path == "tfm.models.gemma" else path
            assert symbols[f"{bert}.BertModel"].canonical == "tfm.models.bert.modeling_bert.BertModel"
            assert f"{bert}.BertLayer" not in symbols
            # Not listed, an older `__all__`, a `modular_*` source beside its generated `modeling_*` file, a script.
            for name in ("GemmaHelper", "Old", "ModularOnly", "main"):
                assert f"{path}.{name}" not in symbols
        assert "tfm.PretrainedConfig" in symbols
        # `_LazyModule` replaces the module: the helper it imported isn't served.
        assert "tfm.define_import_structure" not in symbols
        assert "tfm.models.gemma.define_import_structure" not in symbols
        assert "tfm.plain.define_import_structure" in symbols
        assert "tfm.models.gemma.modular_gemma.ModularOnly" in symbols
        # Only a package that hands `define_import_structure` its own folder.
        assert "tfm.plain.Thing" not in symbols
