import json
import re
from pathlib import Path

from deptime import __version__
from deptime.config import load_coverage

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def test_readme_package_count_matches_coverage():
    """The README states the coverage count once; it has to change in the same commit as coverage.toml."""
    counts = re.findall(r"\b(\d+) fast-moving packages\b", README.read_text(encoding="utf-8"))
    assert counts == [str(len(load_coverage().packages))], "update the count in README.md's Tools table"


def test_registry_server_json_matches_the_release():
    """The MCP Registry rejects a publish unless server.json names this release and the README carries its mcp-name."""
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == __version__
    assert [p["version"] for p in server["packages"]] == [__version__]
    assert re.findall(r"mcp-name: (\S+) ", README.read_text(encoding="utf-8")) == [server["name"]]
