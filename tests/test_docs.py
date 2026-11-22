import json
import re
from pathlib import Path

import pytest

from deptime import __version__
from deptime.config import load_coverage

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SDIST = (ROOT / "PKG-INFO").is_file()


def test_readme_package_count_matches_coverage():
    """The README states the coverage count once; it has to change in the same commit as coverage.toml."""
    counts = re.findall(r"\b(\d+) fast-moving packages\b", README.read_text(encoding="utf-8"))
    assert counts == [str(len(load_coverage().packages))], "update the count in README.md's Tools table"


def test_the_plugin_runs_this_release_and_names_its_icon():
    """Anthropic's plugin directory rejects a launcher that isn't pinned to an exact version and takes the listing's
    icon from the manifest, so the plugin runs the release its manifest names: a release moves both."""
    plugin = ROOT / "integrations" / "claude-code"
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    server = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["deptime"]
    assert manifest["version"] == __version__
    assert (server["command"], server["args"]) == ("uvx", [f"deptime=={__version__}", "mcp"])
    assert (plugin / manifest["icon"]).is_file()
    # The fix-upgrade command (DT-GTM-07) runs the same release; every `deptime==` in it is that release.
    command = (plugin / "commands" / "fix-upgrade.md").read_text(encoding="utf-8")
    pins = re.findall(r"deptime==([\w.]+)", command)
    assert pins and set(pins) == {__version__}


def test_registry_server_json_matches_the_release():
    """The MCP Registry rejects a publish unless server.json names this release and the README carries its mcp-name."""
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == __version__
    assert [p["version"] for p in server["packages"]] == [__version__]
    assert re.findall(r"mcp-name: (\S+) ", README.read_text(encoding="utf-8")) == [server["name"]]


@pytest.mark.skipif(SDIST, reason="action.yml is not included in the sdist")
def test_the_action_runs_this_release_and_keeps_inputs_out_of_scripts():
    """The GitHub Action (DT-GTM-06) installs the release it ships with, so a release moves its pin; and every input
    reaches a script through `env:`, never `${{ }}` inside `run:`, where a value would become shell code."""
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert re.findall(r'default: "deptime==([^"]+)"', action) == [__version__]
    runs = re.findall(r"run: \|\n((?:        .*\n|\n)+)|run: (.*)", action)
    assert runs and all("${{" not in block + line for block, line in runs)


@pytest.mark.skipif(SDIST, reason=".github/workflows is not included in the sdist")
def test_every_workflow_installs_the_index_only_once_its_provenance_is_verified():
    """B2, N1: build-index starts from the published index, then signs what it builds. Without `--verify` it would
    sign a file uploaded by hand; with a fallback after a failed pull, it would build from the snapshots alone and
    drop the releases only the published index keeps."""
    pulls = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\buv run .*deptime index pull", line):
                pulls.append(path.name)
                assert "--verify" in line and "||" not in line, f"{path.name}: {line.strip()}"
    assert {"bench.yml", "build-index.yml", "pages.yml"} <= set(pulls)


@pytest.mark.skipif(SDIST, reason=".github/workflows is not included in the sdist")
def test_a_release_publishes_only_a_tagged_commit_on_main():
    """G1: the `pypi` environment accepts only `v*` tags (a repository setting), but a tag can name a commit on any
    branch, so the publishing job checks that the tagged commit is on `main` before it builds anything."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    pypi = workflow.split("\n  pypi:\n", 1)[1].split("\n  mcp-registry:\n", 1)[0]
    assert "environment: pypi" in pypi
    assert "fetch-depth: 0" in pypi, "the check needs main's history"
    check = re.search(r"git merge-base --is-ancestor \S+ origin/main", pypi)
    assert check and check.start() < pypi.index("uv build") < pypi.index("pypa/gh-action-pypi-publish")
