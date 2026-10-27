# Traceback benchmark (DT-QA-01 / DT-QA-02)

**Purpose:** measure whether `explain_error` gives the right answer on *real* failures. This is the evidence for the launch post and the input to the day-60 accuracy kill criterion (root cause + boundary ≥ 70%, target ≥ 80%).

## Case format (`cases/*.jsonl`, one JSON object per line)

| Field | Meaning |
|---|---|
| `id` | Stable slug |
| `source` | URL of the GitHub issue / Stack Overflow post / migration guide it came from, or `"constructed"` |
| `traceback` | Verbatim text, trimmed of secrets and personal paths |
| `installed` | `{package: version}` the reporter was running |
| `expected.package` | Distribution that changed |
| `expected.symbol_suffix` | End of the expected `finding.symbol`, e.g. `Client.__init__(proxies)` |
| `expected.issue` | `symbol_removed` / `param_removed` / `module_removed` / … |
| `expected.last_version_with` | Optional; checked as a prefix (`"1.5"` matches `1.5.3`) |
| `expected.replacement_contains` | Optional; must appear in one of the top-3 replacement targets |
| `status` | `expected_pass`, or `known_gap` (with the backlog ID that fixes it) |

## Harvesting real cases

Search GitHub issues by error string plus package, for example:
- `"got an unexpected keyword argument 'proxies'" httpx`
- `"cannot import name" langchain is:issue`
- `"has no attribute 'append'" DataFrame`
- `"No module named 'mcp.server.fastmcp'"`

Keep only cases where the reporter's versions are known, or can be inferred from the issue or lockfile. Target ≥ 200 cases across Tier A and Tier B, with at least 3 per Tier A package.

## Scoring (runner: DT-QA-02)

- **Root cause:** `findings[0].package` and `symbol` suffix and `issue` all match.
- **Boundary:** `last_version_with` matches (prefix rule).
- **Replacement:** `replacement_contains` is among the top 3 targets.
- Report per-tier accuracy and the share of `known_gap` cases. CI fails on a regression of more than 2 points.

The seed file (`cases/seed.jsonl`) contains well-documented, real-world breaks. Cases marked `known_gap` say which backlog item fixes them.
