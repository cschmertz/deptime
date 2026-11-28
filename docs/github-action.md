# The upgrade check on pull requests (GitHub Action)

DepTime's GitHub Action runs `deptime check-upgrade` on a pull request that updates a dependency, such as one from
Dependabot or Renovate. It finds the lines of your Python code that the new versions break, writes a report to the
job summary with an annotation on each broken line, and fails the check only on a confirmed break.

It's available from DepTime 0.2.0, and listed on the [GitHub Marketplace](https://github.com/marketplace/actions/deptime-upgrade-check).

```yaml
# .github/workflows/upgrade-check.yml
name: upgrade-check
on:
  pull_request:
    paths: ["**/uv.lock", "**/poetry.lock", "pyproject.toml", "**/requirements*.txt", "**/requirements/**"]
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7 # pin to a commit in your own workflows
      - uses: cschmertz/deptime@v1.2.1
```

## What it does

1. Installs DepTime with `uvx`, pinned to the release the action ships with.
2. Downloads DepTime's index (about 55 MB) from this repository's GitHub releases, and checks its checksum.
3. Reads the version changes between the pull request's base commit and its head, from `uv.lock`, `poetry.lock`,
   `pyproject.toml` or `requirements*.txt` pins (lockfiles and requirements files in any directory,
   `requirements/*.txt` too). It fetches the base commit if the checkout doesn't have it.
4. For each changed package that DepTime covers, it reads every use of that package in the project's Python files and
   checks each one against the index at the new version. A use fails the check when the index shows it failing at the
   new version and not at the old one: a removed name, a keyword that's no longer accepted, an argument that's now
   required, or a call to something that only raises.
5. An update to a release the index doesn't have yet (it is rebuilt twice a day) isn't checked: the summary says
   "Not checked" and a warning marks it, and the step passes. Re-run the job later to check it. When the check can't
   run at all (no index, a lockfile it can't read), the step fails, whatever `fail-on-break` says.

## What it runs, sends and fetches

- **It never runs your code.** DepTime reads your files with Python's `ast` module. It never imports or executes the
  project or the packages it checks.
- **It sends nothing.** It asks no model and calls no service. The only network traffic is from `uvx` installing
  DepTime from PyPI, the index download from GitHub, and `git fetch` of the base commit.
- **It needs no token and no secrets.** `contents: read` is enough, so it works on Dependabot's pull requests, which
  get a read-only token and no secrets.

## Inputs and outputs

| input | default | what |
|---|---|---|
| `path` | `.` | the project's directory, relative to the repository's root |
| `base` | the pull request's base commit | the git revision the old versions are read from |
| `fail-on-break` | `true` | fail the step when a break is found; errors fail it either way |
| `deptime` | the release the action ships with | what `uvx` installs DepTime from |

Outputs: `result` (`pass` or `breaks`) and `report` (the path to the Markdown report).

When it finds a break, the report ends with a collapsed "For a coding agent" block: each broken line, what changed
and the index's proven fix or best replacement, as plain names (no text from the libraries' source), then the uses
DepTime can't tell about, for the agent to check. Paste it to your coding agent, or have one read the report: DepTime
has found what is broken, and the agent writes the fix.

A break you've checked and accept can be left out with a suppression in `deptime.toml` or `pyproject.toml`, as for
the command.

## Posting the report as a comment (optional)

Dependabot's pull requests can't write comments from their own workflow. Don't switch to `pull_request_target`: it
would run the pull request's code with a write token. Upload the report instead, and post it from a second workflow
that runs on `workflow_run`. That workflow checks out nothing, and treats the report as text to post, nothing more.

Add these steps to the check job:

```yaml
      - uses: cschmertz/deptime@v1.2.1
        id: deptime
      - uses: actions/upload-artifact@v7 # pin to a commit in your own workflows
        if: always() && steps.deptime.outputs.report
        with:
          name: deptime-report
          path: ${{ steps.deptime.outputs.report }}
```

And a second workflow:

```yaml
# .github/workflows/upgrade-check-comment.yml
name: upgrade-check-comment
on:
  workflow_run:
    workflows: [upgrade-check]
    types: [completed]
permissions: {}
jobs:
  comment:
    # GitHub lists the pull request only when its branch is in this repository (as Dependabot's are), so a fork's
    # pull request gets no comment.
    if: github.event.workflow_run.event == 'pull_request' && github.event.workflow_run.pull_requests[0] != null
    runs-on: ubuntu-latest
    permissions:
      actions: read
      pull-requests: write
    steps:
      - uses: actions/download-artifact@v8 # pin to a commit in your own workflows
        with:
          name: deptime-report
          run-id: ${{ github.event.workflow_run.id }}
          github-token: ${{ github.token }}
      - env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
          PR: ${{ github.event.workflow_run.pull_requests[0].number }}
        run: gh pr comment "$PR" --repo "$REPO" --body-file deptime-report.md --edit-last --create-if-none
```

The pull request's number comes from GitHub's event, never from the artifact, so a pull request can't make the
comment land somewhere else.

## Limits

- DepTime covers a fixed list of fast-moving Python packages (`deptime coverage` lists them). An update to any other
  package is listed as not checked.
- A break that only shows at run time (a changed return value, different behaviour) isn't in a static index.
- How well it works, measured on 1,129 updates of real projects: `bench/upgrade-check/`.
