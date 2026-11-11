# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Backlog IDs (`DT-…`) point to
[BACKLOG.md](BACKLOG.md), which has the details and the measurements behind each change.

## Versioning

- **Releases** follow [Semantic Versioning](https://semver.org/). What it versions is the contract agents and scripts
  depend on: the MCP tool names and parameters, the fields of every result (`src/deptime/models.py`, also served by
  the HTTP API), and the `deptime` CLI.
- **Contract changes are additive.** A tool, parameter, field, `issue` or event `kind` is never renamed or removed in
  a 0.x patch release; new ones are added. Clients should ignore fields and values they don't know. Before 1.0 a
  minor release may change the contract, and this file says how.
- **Answers can change in any release.** Better extraction or a newer index changes findings, versions and
  replacements; the benchmarks in `bench/` measure each change.
- **The index has its own schema version** (`schema_version` in the index's `meta` table, shown by
  `deptime index status`): `1`, and `2` once a package is stored with its classes' own members (DT-DATA-16). A
  release reads the schema versions it knows and refuses a newer one, keeping the index it has and saying to upgrade:
  0.1.6 and later read `2`. A nullable column added to the index (`releases.requires`, `releases.project_urls`)
  doesn't change the schema version; older releases ignore it.
- `SNAPSHOT_FORMAT` (in `ingest/snapshot.py`) versions the build cache only; changing it rebuilds every snapshot on
  the next build and doesn't affect clients.

## [Unreleased]

### Added
- `check_symbols` says when a call passes more arguments by position than the callable takes at that version
  (`SymbolCheck.max_positional`, given the call's `positional` count), naming the last release that took them:
  pydantic 2's keyword-only `FieldInfo(default)` (DT-CORE-50)
- `diff_versions(used_symbols=...)` lists a used name that no indexed release has, which `check_symbols` reads
  missing at one end of the range or both, in a new field, `never_seen` (with the ends in `missing_in`), and says so
  in `note`: no event can name it. Going from langgraph 0.3.31 down to 0.0.37, `checkpoint.memory.InMemorySaver` comes
  from langgraph-checkpoint at 0.3 and isn't in langgraph 0.0.37's own module (DT-CORE-54)
- `explain_error`'s findings say where the object they name is defined, when that's another path (`defined_at`,
  in the same form as `symbol`): selenium's `webdriver.Chrome.__init__(chrome_options)` is
  `selenium.webdriver.chrome.webdriver.WebDriver.__init__(chrome_options)`, the path in selenium's source. The
  benchmark accepts either path (DT-CORE-55)

### Fixed
- `explain_error` reads a traceback flattened onto one line (`File "...", line 77, in f code AttributeError: ...`),
  an exception class in the middle of a line, and a library's own message quoted without its class, such as numpy
  2's "`np.sctypes` was removed in the NumPy 2.0 release", with or without the backticks (DT-CORE-52)
- The traceback parser no longer takes seconds on some inputs within the 20,000-character limit (one long word, a
  long run of spaces after a frame line): every pattern it runs is linear in the input (DT-CORE-56)
- `explain_error` answers the module an import line asks for when the error names the package it's in: anthropic 0.27
  dropped `types.beta` whole, so `from anthropic.types.beta.tools import ...` fails with "No module named
  'anthropic.types.beta'", and 0.34 brought `types.beta` back without `tools`. The finding is now about
  `anthropic.types.beta.tools`, with a sentence on `types.beta`. IPython's frames now carry the code line their arrow
  marks (`---> 15 from x import y`) (DT-CORE-51)
- A class reads deprecated where its own `__init__` is and the warning names the class: huggingface_hub 0.20 to 0.36
  put `Repository`'s deprecation on its `__init__`, and `check_symbols` read the class as not deprecated.
  `diff_versions` and `symbol_history` give the class a `deprecated` event of its own (`detail.via` `__init__`), and
  `explain_error`'s `deprecated_in` counts it. Not an `__init__` a class inherits, and not a warning about one of its
  arguments or settings (DT-CORE-53)

## [0.1.12] - 2026-11-10

### Added
- `check_symbols` says when a callable's body does nothing but raise (`SymbolCheck.raises`, the exception and its
  message), and `diff_versions` reports the release where it started with a new event kind, `now_raises`
  (DT-DATA-67): llama-index-core 0.14's `ServiceContext.from_defaults` only raises "ServiceContext is deprecated. …",
  so calls to it fail though the name is there. New nullable `raises` columns in the index; older releases ignore
  them. The events are in the `SNAPSHOT_FORMAT` 26 index published 2026-11-10, so `diff_versions` reports them
  without an upgrade; `SymbolCheck.raises` needs this release.
- `diff_versions(used_symbols=...)` reports `added` and `removed` for used names that aren't public API, with
  `detail.internal` (DT-CORE-46): flask 3.0 removed `_app_ctx_stack`.

### Fixed
- A release with no code of its own that pins another distribution at another version is read from that
  distribution's code, which is what installing it imports (DT-DATA-63): dspy 0.1.5 ships only `dspy_dummy` and
  requires `dspy-ai==2.4.5`, so it was never indexed and code asked about it was answered at 0.1.4, an empty
  placeholder, where every name read missing. It now reads dspy-ai 2.4.5's API, `diff_versions` shows what dspy
  2.5 really changed from it, and each tool's `note` says whose code a version's answer comes from. The index part
  is live without an upgrade (2026-11-10); the note needs this release.
- huggingface_hub's top-level functions (`list_models`, `upload_folder` and the rest, bound as `api.list_models`)
  have `HfApi`'s signatures, so a keyword they reject reads as a break (DT-DATA-66); so do streamlit 1.17-1.32's
  `st.line_chart`, `st.dataframe` and the other names bound to `_main`'s methods, ollama's top-level `chat` and
  `generate`, and django's `core.checks.register`. In the `SNAPSHOT_FORMAT` 26 index published
  2026-11-10; no upgrade needed.
- `pandas.__version__` reads present in every release, however a module binds its `__version__` (DT-DATA-65). In the `SNAPSHOT_FORMAT` 26 index published
  2026-11-10; no upgrade needed.
- Names a module `__getattr__` serves by testing `name in dir(module)` read present with their warning: polars 1.x's
  top-level exceptions (`polars.ComputeError`) until 2.0 removes them (DT-DATA-64). huggingface_hub 0.12-0.27's lazy
  loader's private names (`_CACHED_NO_EXIST`) are recorded as internal names. In the `SNAPSHOT_FORMAT` 26 index published
  2026-11-10; no upgrade needed.
- A class gets the properties and attributes its metaclass defines, and a property's deprecation is read from its
  getter (DT-DATA-69): urllib3 1.26's `Retry.DEFAULT_METHOD_WHITELIST` reads removed in 2.0, not never existed;
  pandas' `GroupBy.grouper` (2.2) and `DatetimeIndex.dayofweek` (3.1) read deprecated. Protobuf enums in
  streamlit and qdrant-client get their values (`Alert.Format.ERROR`). In the `SNAPSHOT_FORMAT` 26 index published
  2026-11-10; no upgrade needed.
- `explain_error` reads fastmcp 3's "FastMCP() no longer accepts `log_level`. Pass `log_level` to
  `run_http_async()`" as the removed argument it is, with `run_http_async` as the replacement (DT-CORE-47).
- Replacements for a removed class from its constructor's warning (gradio's `Box` → `Group`), and for a removed
  private module whose classes moved together (huggingface_hub's `utils._errors` → `errors`) (DT-CORE-48). The
  module hints are in the index published 2026-11-10; the class hints need this release.

## [0.1.11] - 2026-11-10

### Fixed
- Names a module still imports, but that its `__all__` (or its being a plain module) leaves out of its public names,
  count as public wherever another release of the package has them as public API (DT-DATA-45): langgraph 0.6's
  `langgraph.channels.base.EmptyChannelError` no longer reads removed, sqlalchemy 2.0's `types.NullType` no longer
  reads added late, and typer's `typer.testing.Result` reads present from 0.8 (click's class). A class member's
  events follow a public name that now names another class: pinecone 9's `PineconeAsyncio` members are compared
  with `AsyncPinecone`'s rather than all reported removed. In the `SNAPSHOT_FORMAT` 23 index published 2026-11-09;
  no upgrade needed.
- Names transformers 5 serves through its import machinery read present (DT-DATA-52): `Qwen2TokenizerFast` and the
  other `XTokenizerFast` names as `XTokenizer`, `XImageProcessorFast` as `XImageProcessor`, and the legacy modules
  `tokenization_utils`, `tokenization_utils_fast`, `image_processing_utils_fast` and
  `models.x.image_processing_y_fast` as their replacements. `coverage.toml` describes such names with
  `[[package.served]]` rules, each holding in the releases whose source has the code that serves them. In the
  `SNAPSHOT_FORMAT` 23 index published 2026-11-09; no upgrade needed.
- transformers 5's tokenizer names that `_LazyModule` serves through `SLOW_TO_FAST_CONVERTERS` read present
  (DT-DATA-58): `ElectraTokenizer`, `LayoutLMTokenizer` and `LxmertTokenizer` and their `Fast` names as
  `BertTokenizer`, LED's, Longformer's and Mvp's as `RobertaTokenizer`, at the top level, in `transformers.models`
  and in their model packages (which read removed in 5.18), and mt5's `T5Tokenizer`. A `[[package.served]]` rule
  can now read a literal mapping from the package's source. In the index published 2026-11-09; no upgrade
  needed.
- More replacements from the package's own deprecation text (DT-CORE-40): "Please refer to X" (sqlalchemy 1.4's
  `Engine.table_names` → `Inspector.get_table_names`); a keyword argument written out in the advice names the
  parameter, not its value (gradio 5.50's `show_copy_button` → `buttons`, not `copy`); and a class whose methods say
  the class is deprecated gets their replacement (transformers' `AutoModelForVision2Seq` →
  `AutoModelForImageTextToText`). In the index published 2026-11-09. A replacement the installed release no longer has
  is followed to its own replacement, which comes first (pydantic-ai 2's `MCPServerHTTP` → `MCPToolset`); in the next
  release.
- Deprecations a library states only in a log message are read (DT-CORE-40): transformers'
  `logger.warning`/`warning_once` calls (`HybridCache` → `StaticCache`; `shard_checkpoint` → huggingface_hub's
  `split_torch_state_dict_into_shards`, from "We recommend you using X from Y library"), and a dataclass's
  `__post_init__` warnings as its `__init__`'s (`TrainingArguments(evaluation_strategy)` → `eval_strategy`). A note
  about a way of passing an argument ("Passing raw bytes as `body` is deprecated") no longer marks the parameter
  deprecated. In the `SNAPSHOT_FORMAT` 24 index published 2026-11-09; no upgrade needed.
- `deptime index pull` reports a failed download (an HTTP error such as GitHub's 504, a network error, a checksum
  mismatch) in one line and exits 1, rather than with a traceback; as before, nothing is installed and the current
  index is kept (DT-OSS-12).
- A name a package imports from a submodule whose name it rebinds reads as the object the package binds, not the
  module (DT-DATA-59): polars' `int_range`, `lit`, `len`, `repeat`, `date_range` and the rest have their
  signatures, so `check_symbols` reads polars 0.19.6's `pl.int_range(n)` as missing `end`; numpy's `memmap` and
  `printoptions` read as the class and the function. In the `SNAPSHOT_FORMAT` 25 index published 2026-11-10; no
  upgrade needed.
- pydantic 2's v1 paths that `pydantic/_migration.py` still serves read present and deprecated, with pydantic's
  message and its target as the replacement (DT-DATA-60): `pydantic.error_wrappers.ValidationError` →
  `pydantic.ValidationError`, `pydantic.generics.GenericModel` → `pydantic.BaseModel`, rather than missing or
  removed in 2.0. A `[[package.served]]` rule can read a table of `module:name` import paths, and the replacement
  miner reads `module:name` and "importing from X instead". In the `SNAPSHOT_FORMAT` 25 index; no upgrade needed.
- A name a module imports by name from the standard library reads present and not public API, with where it comes
  from (DT-DATA-61): `pydantic.networks.IPv4Address` ("pydantic.networks imports it from ipaddress for its own
  use"), not "never seen". `typing`, `typing_extensions`, `collections.abc` and `__future__` names are left out. In
  the `SNAPSHOT_FORMAT` 25 index; no upgrade needed.
- pytest's `_pytest` modules, which plugins import, are indexed as names that aren't public API (DT-DATA-62):
  tavern's `from _pytest._code.code import FormattedExcinfo` reads removed after 9.0.3. `coverage.toml`'s new
  `internal_names` lists such private import roots. Needs the next release and the `SNAPSHOT_FORMAT` 25 index.
- `explain_error` on an attribute of a class DepTime doesn't cover (DT-CORE-43): a member whose class path became
  another covered distribution's class is read as that class's member, at that distribution's installed release.
  langchain-classic's `MultiQueryRetriever.get_relevant_documents` reads langchain-core's
  `BaseRetriever.get_relevant_documents`, removed after 0.3.86, with `invoke`, not a langchain path dropped after
  0.0.340. In the next release.
- `explain_error` on a private attribute of a class no covered package has (DT-CORE-42) looks it up among the
  installed covered packages' members that aren't public API: gplearn's `self._validate_data` reads scikit-learn's
  `BaseEstimator._validate_data`, removed after 1.6.1 (inferred, at most `medium`). In the next release.
- `diff_versions` leaves out removals under a module of a namespace package that a distribution the `to` release
  requires can install, and says so in its note (DT-CORE-41): langgraph 1.x's `langgraph.prebuilt` comes from
  `langgraph-prebuilt`. `check_symbols` already answered `unknown` there. In the next release.
- "Use obj.ffill() or obj.bfill() instead" names the receiver's own methods (DT-CORE-44): pandas'
  `NDFrame.fillna(method)` → `NDFrame.ffill` and `NDFrame.bfill`, sqlmodel's `obj.dict()` → `.model_dump`. In the
  index published 2026-11-10; no upgrade needed.
- A removed module whose public names were all another module's gets that module as its replacement, at 0.75
  (DT-CORE-45): fastmcp 4.0's `fastmcp.server.apps` → `fastmcp.apps`, mcp 1.2's `mcp.server.types` → `mcp.types`,
  matplotlib 3.9's `matplotlib.axes.mcolors` → `matplotlib.colors`. In the index published 2026-11-10; no upgrade
  needed.

## [0.1.10] - 2026-11-08

### Fixed
- `explain_error` on an argument error that names a function without its class (`create() got an unexpected keyword
  argument 'file_ids'`) looks first at the functions of that name whose signature had the argument and that exist in
  the installed release (openai's `Assistants.create`, not the shortest `create`), and, when installed versions are
  given, only at packages installed or required by the raising code (DT-CORE-36).
- mcp 2.x's `mcp.types` (DT-DATA-56): `from mcp_types import *` of the `mcp-types` distribution each release pins at
  its own version is now read from that wheel, so `mcp.types.Tool` and the rest are present in 2.x, and 1.x → 2.x
  reports what really changed (`Tool.inputSchema` → `input_schema`) rather than every type removed. A name under a
  module that star-imports a distribution DepTime doesn't read (google-generativeai's `protos`) reads `unknown`,
  not `missing`. Needs a `SNAPSHOT_FORMAT` 22 index.
- A method's first parameter is the instance or class whatever its name (DT-CORE-39): pydantic models'
  `__init__(__pydantic_self__, **data)` no longer reads as requiring `__pydantic_self__` (`check_symbols`
  `missing_args`), and a `staticmethod` whose first parameter is named `cls` keeps it. Needs a `SNAPSHOT_FORMAT` 22
  index.
- A base class imported through the package's own re-export of another distribution's class is that distribution's
  (DT-DATA-55): langchain-community's `PyPDFLoader.load` reads present, inherited from langchain-core's
  `BaseLoader`. Needs a `SNAPSHOT_FORMAT` 22 index.
- `explain_error` on `cannot import name X` with no module, as Cython-compiled code raises it (no quotes, frames
  without source lines) or as a trimmed paste gives it (DT-CORE-37): the name is looked up among the modules of
  the installed packages (scikit-learn 1.3's `SCORERS` is `sklearn.metrics.SCORERS`, removed after 1.2.2).
- `check_symbols` reads a class's `typing_extensions` base as `typing`'s, and a TypedDict base as `dict`: a name
  that openai's TypedDict params (and 7,000 other such classes) lack reads `missing`, not `unknown`.
- `check_symbols` on a dunder name the index doesn't record (DT-CORE-38): one every module or class has
  (`mcp.__name__`, `Model.__dict__`) reads `present`; another (`polars.__version__`, pydantic's generated
  `__pydantic_serializer__`) reads `unknown` with the reason, unless another release binds it by name.
- matplotlib's `mpl_toolkits` (`mplot3d`, `axes_grid1`, `axisartist`), installed by the same wheel, is indexed with it
  (DT-DATA-57): `'Axes3D' object has no attribute 'w_xaxis'` reads removed in 3.8. Needs a `SNAPSHOT_FORMAT` 22
  index.
- A keyword a function takes in **kwargs and hands to a helper that warns about it reads as accepted and
  deprecated (DT-DATA-52): pydantic-ai 1.95-1.107's `Agent(instrument=)` is accepted up to 1.107, not 1.94. Needs
  a `SNAPSHOT_FORMAT` 22 index.

### Added
- Replacement hints for names renamed in case or underscores under the same parent (`McpError` → `MCPError`).

## [0.1.9] - 2026-11-07

### Added
- Signed build provenance for the index (DT-OSS-09): each published index has a GitHub artifact attestation from
  `build-index.yml`, with its Sigstore bundle published beside it as `index.sigstore.json`.
  `deptime index pull --verify` installs the index only when the attestation names this repository's build workflow
  on `main`, run on a GitHub-hosted runner (it needs the GitHub CLI, `gh`, but no login). `DEPTIME_VERIFY_INDEX=1`
  applies the check to every download, the automatic refresh included, and `deptime index status` shows the run and
  commit that built the installed file.
- `HistoryEvent.affects` (DT-CORE-33): `diff_versions(used_symbols=...)` also reports a used path's parent module or
  class removed, moved or retyped in the range (or added there), listing the used paths it takes with it, so mcp 2.0
  removing `mcp.server.fastmcp` reaches code that uses `mcp.server.fastmcp.FastMCP.tool`.
- `check_symbols` status `unknown` (DT-CORE-32): where the index can't tell, with the reason in `hint`: a path through
  an object that isn't a module or class (`pytest.mark.parametrize`, `django.conf.settings.configure`). A path through
  an alias into another covered package (`fastapi.status.HTTP_200_OK`) is checked in that package, at the newest
  release the package's requirement allows, and a module imported under another path at its own.
- `check_symbols` looks a name a class lacks up in its bases from other distributions (`fastapi.FastAPI.mount` is
  starlette's, `sqlmodel.Session.add` sqlalchemy's), in the standard library's (`enum.Enum`), and reads `unknown` for
  a module missing directly under a namespace package when the release requires a distribution that may install it
  (langgraph-checkpoint installs `langgraph.checkpoint` into langgraph). A class attribute bound to a class
  (`Configure.Generative`), a module attribute bound to one (`masked_array = MaskedArray`) and a nested class have
  their members. These need an index built with `SNAPSHOT_FORMAT` 20, which records each class's bases from outside
  its package (`classes.external`) and each release's namespace packages (`releases.namespaces`) (DT-CORE-32).
- `releases.module_aliases`: paths importable as a module though the index records another object there, those a
  package makes with `sys.modules[...] = ...` (DT-DATA-52) and a submodule whose name its package rebinds (DT-DATA-54);
  `explain_error` reads such a module as present, and counts those releases for one that became only a name (fastmcp
  kept `fastmcp.tools.tool` importable through 3.4).
- `SymbolQuery.positional` and `SymbolCheck.missing_args` (DT-CORE-34): with the call's positional count,
  `check_symbols` lists the required parameters it passes neither by position nor by keyword; `code=` counts them
  from the snippet's calls.

### Changed
- A submodule name its package rebinds (`from .patch import patch`) is recorded as the object it's bound to, still
  importable as the module: `instructor.patch` is the function, with its signature (DT-DATA-54).
- Deprecations a module serves through `__getattr__` are read from a `class __getattr__`'s `deprecated(...)`
  properties (matplotlib's `rcsetup.interactive_bk`) and from a warning helper that is a static or class method of an
  imported class (weaviate's top-level `AuthApiKey`), and reach names a stub already types (DT-DATA-53).
- A class member that switches between class and attribute is no longer a `kind_changed` event: transformers 4.54's
  models declare `config: XConfig` and `PreTrainedModel.__init_subclass__` sets `config_class` from it, so read
  statically the member falls back to the base's `config_class = None`. A module-level class that becomes an alias
  still is one (openai 1.99.2's `ChatCompletionMessageToolCall` union) (DT-CORE-32).
- An argument error's findings rank by the class Python names in the message (its `__qualname__`): `Client.__init__()`
  is httpx's `Client`, not anthropic's, an alias of `Anthropic`; and by a class the caller's line names
  (`pl.DataFrame(...).sort(...)`) (DT-CORE-31).
- `explain_error` reads `No module named 'x.y'` as `module_removed` when the installed release has `x.y` as another
  kind of name and it was a module before (fastmcp 3.2 turned `fastmcp.tools.tool` into a function), and reads a
  library's own exception that says a name was removed (polars 2's `AttributeRemovedError`) as the `AttributeError`
  it stands for (DT-CORE-35).
- `diff_versions(used_symbols=...)` reports a used class member's events at the path given, as `symbol_history` does,
  not at the class that defines it, and leaves out that class's changes that don't reach the member (DT-CORE-33).
- Snapshots load a package's modules in sorted order, so a build on macOS and on Linux resolve re-exports alike
  (DT-DATA-30).
- `diff_versions(used_symbols=...)` leaves out a member's `removed` event when the class still has the name through
  a base from another distribution (sqlmodel's `Session.get`) (DT-CORE-32).
- The `used_symbols` filter reads each path's events by path: 20 transformers paths over 4.30..5.22 took 5 s, now
  19 ms.

## [0.1.8] - 2026-11-07

### Added
- Findings carry `signature_before` and `signature_after`, `links` (both releases on PyPI and the project's changelog
  from its `Project-URL` metadata) and a `fix` the index proves: the same object at another path, or the one
  replacement at confidence 0.75 or more that the installed release has (DT-CORE-29).
- `confidence_factors` on each finding, `explain_error(min_confidence=...)` with `below_min_confidence`, and project
  suppressions in `deptime.toml` or `[tool.deptime]` in `pyproject.toml`, counted in `suppressed` (DT-CORE-30).
- Standard-library names whose presence changes between Python 3.9 and 3.15 (`collections.Mapping`,
  `asyncio.coroutine`, `ssl.wrap_socket`), from typeshed's stubs (DT-DATA-47), with What's New's replacements for
  removed modules and names (DT-DATA-48). The Python version is also read from the file an `ImportError` names.
- `too_many_positional` errors are explained: a parameter that became keyword-only is the new issue
  `param_kind_changed`, with a `fix` of the new kind `keyword` (DT-CORE-09).
- A user's subclass of a library class is linked to it when the evidence allows: an attribute a class named like it
  lost (`'MACAddressField' object has no attribute 'fail'` in marshmallow 4), or a keyword argument the library's own
  code now passes to the user's override (click 8.2's `get_metavar(ctx)`). pytest's and IPython's frame lines are read
  as frames (DT-CORE-02).
- `check_symbols(code=...)` checks what a snippet uses from the package, read with `ast` (DT-CORE-07); `symbols` is
  now optional.
- `python -m deptime.site build` writes rename-map pages for five migrations from the index (DT-GTM-01).
- Answers about a package DepTime doesn't index point to `coverage` and to the package request form;
  `docs/coverage.md` lists what's indexed (DT-OSS-11).

### Changed
- Searching every package for a bare method name (`__init__() got an unexpected keyword argument`) takes one indexed
  lookup per package: 74 ms → under 1 ms for `__init__`.
- transformers' lazy packages export what their folders list (`define_import_structure`), not their stale
  `TYPE_CHECKING` imports: 4.57's SmolLM3, Apertus and 13 more models read present (DT-DATA-05), in the
  `SNAPSHOT_FORMAT` 18 index published 2026-11-07.

## [0.1.7] - 2026-11-06

### Changed
- Reads private class members and the members of private classes, such as weaviate's `_Collections._get_all`
  (DT-DATA-46), and every package's own-member rows quickly, as the `SNAPSHOT_FORMAT` 17 index stores every package
  with its classes' own members (DT-DATA-49, DT-DATA-32, DT-DATA-28).

## [0.1.6] - 2026-11-05

### Added
- Names a module binds that aren't public API, flagged `internal`, so a removed one reads `symbol_removed` with its
  versions (DT-DATA-22).
- Standard-library modules per Python version, from typeshed's `stdlib/VERSIONS` (DT-DATA-06).
- `coverage` gives each package's `latest_released_at`, and an installed release newer than the index is answered as
  such (DT-DATA-04).

### Changed
- Reads schema 2 indexes, which store each class's own members once (DT-DATA-16); 0.1.5 and older refuse them and
  keep the index they have.
- An import name another distribution also uses (`agents`) is told apart by the installed versions (DT-CORE-03).
- The index is replaced safely on Windows too, also while the old file is open (DT-OSS-08).

## [0.1.5] - 2026-11-05

### Added
- `last_version_without` and `first_version_with` on `*_not_yet_added` findings (DT-CORE-18).

### Changed
- Reads chained exceptions, `ExceptionGroup` sub-exceptions and lazy-loader messages (DT-CORE-25); a removed
  submodule reads `module_removed`, and Python 3.9's `__init__()` messages take the class from the call (DT-CORE-26).
- An error raised inside a library searches the packages that library requires first, and faster (DT-CORE-16).
- A failed first index download is retried with backoff, and answers say why there is no index (DT-OSS-10).

## [0.1.4] - 2026-11-01

### Changed
- Downloads the `.xz` index, 37% smaller than the `.gz` (59.2 MB against 94.7 MB when it shipped; DT-DATA-35).
- Offers the replacement an error message names, once the index has it (DT-CORE-06), and reads numpy 2.0-2.3's
  class-member removal messages (DT-CORE-21).

## [0.1.3] - 2026-10-29

### Changed
- The tool parameters carry descriptions in their schemas (DT-GTM-03).

## [0.1.2] - 2026-10-28

### Added
- Published to the official MCP Registry as `io.github.cschmertz/deptime` (DT-GTM-03).

## [0.1.1] - 2026-10-28

### Changed
- The PyPI page shows the current README (DT-OSS-02).

## [0.1.0] - 2026-10-28

### Added
- The local stdio MCP server (`deptime mcp`, `uvx deptime mcp`) with five read-only tools: `explain_error`,
  `symbol_history`, `check_symbols`, `diff_versions` and `coverage`.
- The prebuilt index of 52 packages, downloaded on first run and refreshed daily; `deptime index pull|status|pack`;
  offline mode (`DEPTIME_OFFLINE=1`).

[Unreleased]: https://github.com/cschmertz/deptime/compare/v0.1.12...HEAD
[0.1.12]: https://github.com/cschmertz/deptime/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/cschmertz/deptime/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/cschmertz/deptime/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/cschmertz/deptime/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/cschmertz/deptime/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/cschmertz/deptime/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/cschmertz/deptime/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/cschmertz/deptime/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/cschmertz/deptime/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/cschmertz/deptime/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/cschmertz/deptime/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/cschmertz/deptime/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/cschmertz/deptime/releases/tag/v0.1.0
