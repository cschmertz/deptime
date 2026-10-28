import re
from pathlib import Path

from deptime.config import load_coverage

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_package_count_matches_coverage():
    """The README states the coverage count once; it has to change in the same commit as coverage.toml."""
    counts = re.findall(r"\b(\d+) fast-moving packages\b", README.read_text(encoding="utf-8"))
    assert counts == [str(len(load_coverage().packages))], "update the count in README.md's Tools table"
