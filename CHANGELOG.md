# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Backlog IDs (`DT-…`) point to
[BACKLOG.md](BACKLOG.md) and, for work finished before 2026-11-13, to
[docs/backlog-archive.md](docs/backlog-archive.md), which has the details and the measurements behind each change.

## Versioning

Releases follow [Semantic Versioning](https://semver.org/). From 1.0.0 on, this is the promise.

**What it versions:** the contract agents, scripts and CI depend on.
- The six MCP tools (`explain_error`, `check_symbols`, `diff_versions`, `symbol_history`, `check_upgrade`,
  `coverage`): their names, their parameters and what each means. The HTTP API serves the same tools at `/v1/<tool>`
  and `/mcp`.
- Every field of every result (`src/deptime/models.py`) and the values listed for it (a finding's `issue`, an
  event's `kind`, a check's `status`, ...). The CLI prints the same JSON without the empty (`null`) fields.
- The `deptime` commands `mcp`, `explain`, `history`, `check`, `diff`, `check-upgrade`, `coverage`, `index pull`,
  `index status` and `serve`, with their options, and `check-upgrade`'s exit codes: 0 no break, 1 a confirmed
  break, 2 the check couldn't run (a bad option or directory, a revision or pin it couldn't read).
- The GitHub Action's inputs and outputs (`action.yml`), and the Claude Code plugin's `/deptime:fix-upgrade`
  command.
- The suppressions file (`deptime.toml`, or `[tool.deptime]` in `pyproject.toml`), and the settings a user sets:
  `DEPTIME_HOME`, `DEPTIME_OFFLINE`, `DEPTIME_INDEX_PATH`, `DEPTIME_INDEX_URL`, `DEPTIME_INDEX_MAX_AGE_HOURS`,
  `DEPTIME_VERIFY_INDEX`, `DEPTIME_COVERAGE_FILE` and `DEPTIME_PROJECT_DIR`.

**What can change in any release:**
- **Answers.** Better extraction or a newer index changes findings, versions and replacements; the benchmarks in
  `bench/` measure each change.
- **Words.** Tool descriptions and the server's instructions (re-benchmarked when edited), and the text of `note`,
  `explanation`, `hint` and the other sentences in a result.
- **The Markdown report** of `check-upgrade` and its agent block, and what `/deptime:fix-upgrade` tells the agent
  (re-measured when reworked, as DT-QA-05, DT-QA-26 and DT-QA-27 did). Scripts read `--format json`.
- **Tools for maintainers and hosts:** `deptime ingest`, `deptime index pack`, and `serve`'s hosted-mode settings
  (usage limits, API keys, proxy headers; [docs/05](docs/05-self-hosting.md)). A change is noted here.

**How it changes:**
- **Only by adding, within 1.x:** a tool, an optional parameter, a field, a listed value or a command option.
  Clients should ignore fields they don't know, and not fail on a value they don't know.
- **Retiring something** takes a major release. It is first marked deprecated, in its description and under
  `Deprecated` here, in a minor release, and keeps working for the rest of 1.x.

**Python:** DepTime runs on Python 3.12 and newer, each tested in CI (3.12 to 3.15). A Python is dropped only after its end
of life, in a minor release, noted here. (The Pythons DepTime answers about, the standard library's changes since 3.9,
are data: they can grow in any release.)

**The index:**
- It has its own schema version (`schema_version` in the index's `meta` table, shown by `deptime index status`):
  `1`, and `2` once a package is stored with its classes' own members (DT-DATA-16). Every 1.x release reads `2`.
  A release refuses a newer schema, keeps the index it has and says to upgrade. A nullable column added to the index
  (`releases.requires`, `releases.project_urls`) doesn't change the schema version; older releases ignore it.
- **During 1.x the published index stays readable by every 1.x release.** A change none of them could read waits
  for 2.0, or is published beside the current index, not in its place.
- It is rebuilt from PyPI twice a day and signed (`deptime index pull --verify`). That's a schedule, not an uptime
  promise: if builds stop, the index you have keeps answering, and `coverage` says when it was built.
- Packages are added in any release. A covered package is dropped only with a note here; it then reads
  `not_covered`.
- `SNAPSHOT_FORMAT` (in `ingest/snapshot.py`) versions the build cache only; changing it rebuilds every snapshot on
  the next build and doesn't affect clients.

**Before 1.0:** a 0.x minor release could change the contract, and this file said how; a patch release only added.

## [Unreleased]

### Added
- `diff_versions` says which used names it can't check, and why, in a new `unchecked_uses` field, and its note
  points to it, instead of an empty answer that reads as "nothing changed" (DT-CORE-61).
- `diff_versions` lists the used paths already deprecated at `to_version` in a new `deprecated_uses` field, with
  `deprecated_in` and the library's message, wherever the deprecation began, and a `deprecated` event inside the
  range says when the deprecation ended in `detail.until` (DT-CORE-62).
- Migration hints for gradio 4.0: `gradio.inputs` and `gradio.outputs` → `gradio.components` (quoting the 3.x
  warnings in gradio's own `inputs.py` / `outputs.py`), `gradio.Variable` → `gradio.State`, and the `source` argument
  of `gr.Audio` / `gr.Video` → `sources`, the last three from the 4.0.0 entry of gradio's CHANGELOG.
- Migration hints for polars 2.0, taken from the removal table in its upgrade guide: `melt()` → `unpivot()`,
  `with_row_count()` → `with_row_index()`, `pl.Array.width` → `size`, `read_csv_batched()` → `scan_csv()`, and the
  argument renames (`dtypes` → `schema_overrides`, `row_count_name` → `row_index_name`, `join_nulls` →
  `nulls_equal`, `descending` → `reverse`, and the rest), 52 hints in a `# polars-2` group. Rows whose replacement is
  prose or a workaround are left out.
- A migration hint for weaviate-client 4.10: the removed v3 `weaviate.Client` → `weaviate.WeaviateClient`, quoting
  the message the old class now raises (DT-CORE-77).

### Fixed
- A word in a deprecation message is no longer read as its replacement: weaviate's "use Python client v4" and "use
  v3 code" gave `weaviate.Client` the replacements "Python" and "v3". A name where a stored message was cut off is
  skipped too, as it may go on (DT-CORE-77).
- Two more ways libraries word a replacement are read: SQLAlchemy 1.4's "Use the generative method
  Select.where()" gives the `Select.append_*` methods their replacements, and gradio 3.x's "`return gr.Audio(...)`
  instead of `return gr.Audio.update(...)`" points each component's removed `update` method to the component
  (DT-CORE-78).

## [1.0.3] - 2026-11-17

### Fixed
- `check-upgrade`'s report and its agent block no longer hide what replaces a name that went away with its module
  or class: updating mcp 1.x to 2.x now says `mcp.server.fastmcp.FastMCP` is `mcp.server.mcpserver.MCPServer`
  instead of only that `mcp.server.fastmcp` is gone. The agent block names each line once (DT-CORE-76).

### Changed
- The README leads with checking a dependency update, with a demo drawn from a real `check-upgrade` run (DT-GTM-10).
- The rename maps are published at https://cschmertz.github.io/deptime/, rebuilt after every index build, with a
  landing page and a new httpx 0.27 → 0.28 map (DT-GTM-02).

## [1.0.2] - 2026-11-17

### Fixed
- `check_symbols` and `check-upgrade` read a member of a name bound to a union of classes as missing, with the
  reason: mcp 2.0's `types.ClientRequest` is `PingRequest | ...`, so `ClientRequest.model_construct` fails
  (DT-CORE-73). Earlier releases answer `unknown` on the same index.

### Changed
- The GitHub Action's description is shorter, as GitHub Marketplace requires.

## [1.0.1] - 2026-11-17

### Changed
- The README is rewritten in plain language, so the project page on PyPI matches GitHub's. No code changed.

## [1.0.0] - 2026-11-17

The first stable release (DT-OSS-17). The tools, results and commands are the same as 0.2.3; what's new is the
promise in [Versioning](#versioning) above: within 1.x, nothing in the contract is renamed or removed.

### Added
- Python 3.14 and 3.15 are supported and tested in CI, beside 3.12 and 3.13.
- Descriptions for three result fields that had none: a traceback signal's `kind`, a covered package's `tier`, and
  the coverage `policy`.

### Checked
- First launch, from nothing to the right answer, on each operating system (DT-SPIKE-07, `bench/s7/`): 8 s on
  Linux, 10 s on Apple-silicon macOS, 20 s on Windows. macOS with an x86_64 `uv` takes about 3 minutes, compiling
  `cryptography`; run `uvx deptime --help` once first.

## [0.2.3] - 2026-11-16

The Claude Code plugin's fix command, reworked again from DT-QA-26, and streamlit's top-level calls checked again;
nothing in the contract changed.

### Changed

- `/deptime:fix-upgrade` (Claude Code plugin) rewrites a break that has no replacement from the new version's source
  or release notes, leaving it only when the new version can't do what the code did, and fixes any other break it
  finds; `check-upgrade`'s "For a coding agent" block says the same, and now also shows when the only uses left are
  ones DepTime can't tell about (DT-GTM-09).

### Fixed

- streamlit 1.33 and later: `st.pyplot`, `st.button`, `st.write` and 100 other top-level names, bound through an
  annotated module-level instance, read as attributes with no signature, so `check_symbols` and `check-upgrade`
  didn't check their calls. They take the method's signature again from the next index build (`SNAPSHOT_FORMAT` 28,
  DT-DATA-83).

## [0.2.2] - 2026-11-16

The Claude Code plugin's fix command, reworked from DT-QA-05; nothing in the contract changed.

### Changed

- `check-upgrade`'s "For a coding agent" block lists the uses DepTime can't tell about and asks the agent to check
  them, instead of saying they aren't breaks; the markdown report's section for them says the same (DT-GTM-08).
- `/deptime:fix-upgrade` (Claude Code plugin) writes the report to a file and reads it, so a long report is read
  whole; reruns with `--from` when the project pins the package somewhere DepTime doesn't read; and has Claude check
  the uses DepTime can't tell about, even when the check finds no break (DT-GTM-08).

## [0.2.1] - 2026-11-16

Two fixes to the upgrade check; nothing in the contract changed.

### Changed
- `check-upgrade` reads `uv.lock` and `poetry.lock` in any directory, as it does requirements files, so an update to
  one project of a monorepo (`backend/uv.lock`) is checked; a lockfile's own project isn't read as a dependency. The
  Action's documented trigger paths match them (DT-CORE-74)
- `check_upgrade`'s `note` says when the code passed never imports the package, so no use was found: such code used
  to read as no break with `uses: 0` and nothing else (DT-CORE-75)

## [0.2.0] - 2026-11-16

The upgrade check: what a dependency update breaks in a project's code, decided by the index alone, with no model
in the loop. A minor release because the contract grows by a tool, a CLI command and their result model; nothing
was renamed or removed. On 264 real Dependabot and Renovate pull requests, scored once before any fix, it failed
all 13 whose update breaks the project's code and flagged 1 of 3,291 working uses
(`bench/upgrade-check/prs/results-2026-11-16.md`).

### Added
- `deptime check-upgrade [path]`: what a dependency update breaks in a project's code. It reads the versions before
  and after from `uv.lock`, `poetry.lock`, `pyproject.toml` or `requirements*.txt` pins (requirements files in any
  directory; a git revision, `--base`, against the working tree; or `--package P --to V`), reads every Python file
  with DepTime's own reader, and checks each use of a changed covered package at the new version. Each break comes
  with its file and line, the release that made the change and the replacement; uses deprecated at the new version
  are warnings, and uses DepTime can't tell about, or that fail at the old version too, are listed apart. Markdown
  or JSON; exit code 1 only on a break.
  Nothing is installed, imported or run, and no model is asked (DT-CORE-58, DT-CORE-66, DT-CORE-59)
- A sixth MCP tool, `check_upgrade(package, from_version, to_version?, code?, files?)`: the same check for one
  package as one call, on the code an agent passes (a snippet, or files by path), for clients with no shell. Also
  `POST /v1/check-upgrade` (DT-CORE-68)
- A GitHub Action (`uses: cschmertz/deptime@v0.2.0`, `action.yml`): runs the check on a pull request against its
  base, annotates each broken line, writes the report to the job summary and fails only on a break. It needs
  `contents: read` and no secrets, so it works on Dependabot's pull requests (`docs/github-action.md`, DT-GTM-06)
- When there's a break, the report ends with a "For a coding agent" block: each broken line and the index's proven
  fix or best replacement, as plain names. The Claude Code plugin's `/deptime:fix-upgrade` runs the check and asks
  Claude to fix exactly those lines, then runs it again (DT-GTM-07)

### Changed (index, published by the build, not by this release)
- A `@dataclass(init=False)` class keeps the constructor it inherits (pydantic-ai 2's `RequestUsage(input_tokens=)`
  read rejected); a class or function defined only when an import fails isn't read as only raising (dspy 3.4.1's
  `GEPA`); names a module binds through `global` in a function it runs at import are found (sqlalchemy 2.x's
  `orm.collections.attribute_mapped_collection`). `SNAPSHOT_FORMAT` 27 (DT-CORE-72, DT-DATA-82, DT-DATA-51)

## [0.1.17] - 2026-11-15

The first code change since 0.1.13: `explain_error` and `check_symbols` read more of what people and models send.

### Fixed
- `check_symbols` reads `kwargs` written as a call's shape: `timeout=` is the keyword `timeout`, and a lone `_` (an
  argument passed by position) is left out instead of rejected. The `note` says what it read. A small model copied
  that notation in 55 of its 315 calls in DT-QA-20, and the rejections fed its false alarms (DT-CORE-67)
- `explain_error` reads three more errors as a web page shows them: a keyword in backticks, a `cannot import name`
  message broken across two lines, and Django's `Module "x" does not define a "Y" attribute/class`. `X.init()`, a
  Markdown-eaten `X.__init__()`, is also tried as `__init__`; when several classes of one name changed the same way
  and nothing in the text says which, the `note` lists them (DT-CORE-63)
- A keyword no release named, which a `**kwargs` took until a release dropped it, reads `param_removed` at that
  release (at `medium`), not `never_existed`: mcp 1.12.3's `FastMCP(description=...)` (DT-CORE-64)

### Changed
- The Claude Code plugin runs `uvx deptime==0.1.17`

## [0.1.16] - 2026-11-15

A documentation release: the code is 0.1.13's, and every answer is the same.

### Added
- The upgrade checks get the same comparison as the tracebacks: the 2026-11f holdout's 2,265 labelled uses put to a
  model alone, protocol committed first. Claude Sonnet 5.5 finds 91% of the breaks and flags 0.2% of working code,
  beside DepTime's 96.7% and 0.1%; Haiku 5.5 74% and 0.3% (DT-QA-18)
- Two cheaper models from other vendors on both comparisons, through OpenRouter: DeepSeek V4.1 Flash and Qwen3.5 9B
  score 59% and 19% on the tracebacks (DepTime 87%), and on the upgrade checks flag 2.7% and 15% of working code. The
  README's table gains a row for the upgrade checks and a line on what this shows: the smaller the model, the more
  DepTime adds (DT-QA-19)

### Changed
- The Claude Code plugin runs `uvx deptime==0.1.16`

## [0.1.15] - 2026-11-14

A documentation release: the code is 0.1.13's, and every answer is the same.

### Added
- A comparison for the traceback score: the same 191 holdout tracebacks put to a language model with no tools,
  under a prompt and scoring rule committed before the run. Three Claude models name the right package, name and
  breaking release for 66% to 84%, beside DepTime's 87%; they name the change and its replacement more often than
  DepTime, and the release less often. The README has the row, and `bench/model-alone/` the protocol, every reply
  and the results (DT-QA-16)

### Changed
- The Claude Code plugin runs `uvx deptime==0.1.15`, pinned to the plugin's own version as Anthropic's plugin
  directory requires, and its manifest names its icon. Its README says what the hint hook keeps on disk: a short
  hash of each error line it has hinted at, per session, and no error text (DT-GTM-04)

## [0.1.14] - 2026-11-14

A documentation release: the code is 0.1.13's, and every answer is the same.

### Changed
- The README, which is also the PyPI page, is rewritten for a first-time reader. It opens with an animated demo
  drawn from a real answer, gives four first-run numbers under "How well it works" with the full table in
  `bench/README.md`, and says what DepTime doesn't do (DT-OSS-02)
- The project is in maintenance mode: the index still rebuilds twice a day, and fixes follow reported wrong
  answers. `BACKLOG.md` is the short plan; the record of earlier work is `docs/backlog-archive.md`

## [0.1.13] - 2026-11-11

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

[Unreleased]: https://github.com/cschmertz/deptime/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/cschmertz/deptime/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/cschmertz/deptime/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/cschmertz/deptime/compare/v0.2.3...v1.0.0
[0.2.3]: https://github.com/cschmertz/deptime/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/cschmertz/deptime/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/cschmertz/deptime/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/cschmertz/deptime/compare/v0.1.17...v0.2.0
[0.1.17]: https://github.com/cschmertz/deptime/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/cschmertz/deptime/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/cschmertz/deptime/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/cschmertz/deptime/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/cschmertz/deptime/compare/v0.1.12...v0.1.13
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
