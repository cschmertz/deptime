# Per-release holdout (DT-QA-07)

**Purpose:** measure DepTime on cases nobody designed a fix from, including code that still works after an upgrade.
Every case in `bench/cases` and `bench/tmb` has a real break, so they measure how often DepTime finds a break, never how
often it flags code that is fine. And once fixes are designed from a set's misses, it stops being a holdout.

So before each minor release, a new set is collected under the rules below and **committed before DepTime runs on it**.
It is scored once; that first score is the number to quote, and leads the README's table. Then its tracebacks join
`bench/cases` and its uses stay here as a regression check. A fix designed from a set's misses is scored on the next set,
not this one.

## What a set holds

`<set>/` (e.g. `2026-11/`):

| File | What |
|---|---|
| `upgrades.jsonl` | One line per upgrade: a real project, a covered package, `from` and `to` versions, and the uses of the package its code has (`uses.py`) |
| `envs.json`, `envs-tracebacks.json` | The environments the probe installs for the uses and for the tracebacks' boundaries (`envs.py`) |
| `labels.jsonl`, `probe-envs.json` | Each use's label from the probe (`labels.py`), and what each environment installed |
| `label-overrides.json` | Labels changed by hand, each with the library source that shows it |
| `tracebacks.jsonl`, `traceback-checks.json` | Real tracebacks, in the `bench/cases` format, and the probe's check of each boundary (`tbcheck.py`) |
| `selection.json` | How the upgrades were picked: the search, every candidate and why it was skipped |
| `results-<date>.md` | The first scoring |

### Uses: code that works and code that breaks

A **use** is something a project's code does with a covered package, read from its source with `ast` (`uses.py`, which
shares no code with DepTime's own reader): a name it imports or reads (`numpy.linalg.norm`), a call with its shape
(positional count and keywords: `httpx.Client(timeout=)`), or the same on an object the code built by calling a class
(`client = httpx.Client()`, then `client.get(_, params=)` is `httpx.Client.get`). The package's root module isn't a use.
Calls that splat (`*args`, `**kw`) count as names only. At most 40 uses per upgrade, picked by hash, so one large project
can't dominate.

Two sources:

1. **TimeMachine-bench** (its verified subset, dataset commit `9928dbf`; see `bench/tmb/README.md`): 100 repositories
   whose tests pass under their original dependencies and, after the gold fix, under PyPI as of 2025-07-31. Each covered
   package the migrated environment pins is an upgrade: `to` is that pin, `from` the package's newest release on PyPI on
   the repository's reproduction date. Their breaks were in DT-QA-04 and fixes were designed from them, so **only uses
   that work count** (and none named in the gold patch).
2. **Pinned repositories** (`find_pinned.py`): for each package TimeMachine-bench barely covers (every Tier A package,
   and the Tier B ones with fewer than three of its upgrades), GitHub code search for `<package>==` in requirements
   files, in the order it returns them. A repository qualifies when it isn't a fork or the package itself, pins a final
   release from the package's first indexed release up to but not including its newest release on PyPI, and its source
   at the default branch's head uses the package at least three times. The first two qualifying repositories are kept.
   Each gives two upgrades from the pin: to the newest release (`up`), and to the newest release at least a year older
   than the pin when that is indexed (`down`: code written for a newer release than the one installed).

### Labels: the probe, never DepTime

`.github/workflows/holdout.yml` installs each package version in a throwaway venv on a GitHub runner (wheels only; PyPI
as of 30 days after the version's release, so its dependencies are ones it shipped with; the newest releases as of the
set's cutoff; TimeMachine-bench's `to` as of 2025-08-01 on Python 3.12) and, with no network as an unprivileged user,
checks each use at run time (`probe.py`): does the name resolve, and does the call shape bind to `inspect.signature`?
A path resolves when reading attributes down from the top package works (importing a submodule where an attribute is
missing), as `from a import b` and `a.b.c` do, or else when importing its longest module prefix and reading the rest
works, which finds a module a package registers in `sys.modules` under a name an attribute shadows.

`labels.py` turns that into one label per use and upgrade:

- **works**: it resolves (and binds) in `to`.
- **breaks**, with its change type: `removed`, `param_removed`, `param_now_required`, `positional`, or, for `down`
  upgrades, `symbol_not_yet_added` and `param_not_yet_added`.
- **excluded** when the probe can't tell: an import failing for another reason (a missing optional dependency), no
  signature, a keyword that only `**kwargs` takes, an object built by a call that isn't a class, or a member of a built
  object that was a property or attribute on the class in `from` and is missing from it in `to` (each instance may set
  it in `__init__`, which reading the class can't see); or the use doesn't work in `from` either (pinned); or, for
  TimeMachine-bench, it fails in `to` although the tests passed (unexercised code or not a real path), or the gold patch
  names it.

Breaks found by binding a call (a keyword not accepted, an argument now required) are checked by hand against the
library's source at `to`, because a generated signature can disagree with what `__init__` takes (a pydantic model's is
built from its fields). A use the source shows still works goes into `label-overrides.json` and is excluded; the ones
checked and kept are listed there too.

A use that works is also **deprecated** when reading it in `to` raises a deprecation warning, or the object carries
`__deprecated__` or a deprecation note in its docstring.

### Tracebacks

Real tracebacks under the DT-QA-01 rules (`bench/README.md`), collected for this set: found in GitHub issues, each
traceback matched against its page, each stated version quoted, each expected answer taken from the library's changelog,
migration guide or source at the two releases. Where the probe can import the name (no torch, no class-level view of an
instance attribute), a case's `check` names the failing use and the probe confirms it works in the last release with it
and fails in the first release without it and at the installed version (`tbcheck.py`); the others were checked in the
source at both tags.

## Scoring (fixed before the first run)

```bash
uv run deptime index pull --verify
uv run python bench/holdout/score.py bench/holdout/<set> --json <scratch>/holdout.json > bench/holdout/<set>/results-<date>.md
```

Before that, each set is built and committed with:

```bash
uv run python bench/holdout/collect.py tmb <verified.jsonl> <sources> <tmb.jsonl>          # TimeMachine-bench upgrades
uv run python bench/holdout/find_pinned.py <packages> <sources> <pinned.json>               # pinned repositories
uv run python bench/holdout/collect.py pinned <pinned.json> <sources> <pinned.jsonl>
cat <tmb.jsonl> <pinned.jsonl> > bench/holdout/<set>/upgrades.jsonl                          # plus tracebacks.jsonl
uv run python bench/holdout/envs.py bench/holdout/<set>                                      # then push spike/holdout
uv run python bench/holdout/labels.py bench/holdout/<set> <artifacts>
uv run python bench/holdout/tbcheck.py bench/holdout/<set> <artifacts>
```

Each labelled use is asked two ways, as an agent would ask it:

- `check_symbols(package, version=to, symbols=[{path, kwargs}])`: **flagged** when the status is `missing` or a keyword
  the call passes is in `rejected_kwargs`. `not_indexed` is an abstention.
- `diff_versions(package, from, to, used_symbols=[path], include_added=True)` (`down`: from `to` up to `from`):
  **flagged** when the events say `to` lacks what the use needs: the path's last membership event before `to` is
  `removed` (for `down`, its first one after `to` is `added`); for a call, the same for each keyword it passes on the
  path or its `__init__` (`param_removed`/`param_added`), and, upgrading, a parameter that became required and isn't
  passed by keyword. An event on a parent that lists the path in `affects` (a removed module or class, DT-CORE-33)
  counts as one on the path; sets scored before it (2026-11) didn't have the field. An unresolvable range (a `from`
  older than the index) is an abstention.

Then, for each tool: **precision** (flagged uses that break), **recall** (breaking uses flagged) and **false-positive
rate** (working uses flagged), each with a 95% Wilson interval, overall and by change type (recall), tier, package,
source and kind of use; the share of upgrades where every use works that get any false alarm; and deprecation flags
against the probe's evidence. Abstentions are reported, not counted.

Tracebacks are scored as `bench/README.md` defines (root cause, + boundary, replacement), with intervals, and precision:
of the cases where `explain_error` gives a finding, how many name the right root cause. They are also split by change
type (a removal whose library states a replacement counts as **moved**, a removed parameter with one as **renamed**;
then removed, parameter removed, now required, not yet added), by tier, and by whether the same break is already in
`bench/cases`.

**Deprecated** is measured on uses only: tracebacks are breaks, and a deprecated name still works.

Uses within one project aren't independent, so the intervals are narrower than they should be; the per-upgrade false
alarm share is the check on that.
