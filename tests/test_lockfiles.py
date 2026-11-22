"""Reading a project's pinned versions and what changed between two revisions (DT-CORE-66)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from deptime.lockfiles import Change, Pins, changes, project_name, read_pins

UV_LOCK = """version = 1
requires-python = ">=3.11"

[[package]]
name = "httpx"
version = "0.27.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "Pydantic_Core"
version = "2.20.1"

[[package]]
name = "my-project"
version = "0.1.0"
source = { editable = "." }
"""

POETRY_LOCK = """[[package]]
name = "numpy"
version = "1.26.4"
description = "Fundamental package for array computing in Python"
optional = false

[metadata]
lock-version = "2.0"
"""


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_lockfiles_give_every_resolved_version(tmp_path):
    _write(tmp_path, {"uv.lock": UV_LOCK, "poetry.lock": POETRY_LOCK})
    pins = read_pins(tmp_path)
    assert pins.by_file["uv.lock"] == {"httpx": "0.27.2", "pydantic-core": "2.20.1"}  # not its own project
    assert pins.by_file["poetry.lock"] == {"numpy": "1.26.4"}
    assert pins.versions("Pydantic-Core") == {"uv.lock": "2.20.1"}


def test_requirements_pins_only_exact_versions(tmp_path):
    _write(
        tmp_path,
        {
            "requirements.txt": "\n".join(
                [
                    "# a comment",
                    "httpx==0.27.2  # pinned",
                    "Requests[socks]==2.31.0 ; python_version >= '3.8'",
                    "numpy>=1.20",
                    "pandas~=2.0",
                    "flask==2.*",
                    "-e .",
                    "--index-url https://example.invalid/simple",
                    "torch===2.1.0",
                    "openai==1.40.0 \\",
                    "    --hash=sha256:abc",
                    "git+https://github.com/x/y.git",
                    "-r requirements/base.txt",
                    "-r ../outside.txt",
                ]
            ),
            "requirements/base.txt": "pydantic==2.8.0\nhttpx==0.20.0\n",
            "requirements-dev.txt": "pytest==8.0.0\n",
        },
    )
    pins = read_pins(tmp_path)
    assert pins.by_file["requirements.txt"] == {
        "httpx": "0.27.2",  # its own pin wins over the included file's
        "requests": "2.31.0",
        "torch": "2.1.0",
        "openai": "1.40.0",
        "pydantic": "2.8.0",
    }
    assert pins.by_file["requirements/base.txt"] == {"pydantic": "2.8.0", "httpx": "0.20.0"}
    assert pins.by_file["requirements-dev.txt"] == {"pytest": "8.0.0"}


def test_pyproject_pins_from_project_groups_and_poetry(tmp_path):
    _write(
        tmp_path,
        {
            "pyproject.toml": """
[project]
name = "My_Project"
dependencies = ["httpx==0.27.2", "numpy>=1"]
[project.optional-dependencies]
ai = ["openai==1.40.0"]
[dependency-groups]
dev = ["pytest==8.0.0", {include-group = "lint"}]
lint = ["ruff>=0.5"]
[tool.poetry.dependencies]
python = "^3.11"
pandas = "2.2.2"
polars = "==1.0.0"
langchain = {version = "0.2.1", extras = ["openai"]}
scipy = "^1.11"
[tool.poetry.group.test.dependencies]
hypothesis = "6.100.0"
""",
        },
    )
    assert read_pins(tmp_path).by_file["pyproject.toml"] == {
        "httpx": "0.27.2",
        "openai": "1.40.0",
        "pytest": "8.0.0",
        "pandas": "2.2.2",
        "polars": "1.0.0",
        "langchain": "0.2.1",
        "hypothesis": "6.100.0",
    }
    assert project_name(tmp_path) == "my-project"


def test_a_broken_file_is_reported_not_guessed(tmp_path):
    _write(tmp_path, {"uv.lock": "this is [ not toml", "requirements.txt": "httpx==0.27.2\n"})
    pins = read_pins(tmp_path)
    assert "uv.lock" not in pins.by_file and pins.by_file["requirements.txt"] == {"httpx": "0.27.2"}
    assert pins.problems and pins.problems[0].startswith("uv.lock: TOMLDecodeError")


def test_changes_once_per_package_and_versions():
    base = Pins(
        {
            "uv.lock": {"httpx": "0.27.2", "gone": "1.0", "same": "2.0"},
            "requirements.txt": {"httpx": "0.27.2"},
        }
    )
    head = Pins(
        {"uv.lock": {"httpx": "0.28.1", "new": "1.0", "same": "2.0"}, "requirements.txt": {"httpx": "0.28.1"}}
    )
    assert changes(base, head) == [
        Change("httpx", "0.27.2", "0.28.1", "requirements.txt"),
        Change("gone", "1.0", None, "uv.lock"),
        Change("new", None, "1.0", "uv.lock"),
    ]
    assert changes(base, head, {"httpx"}) == [Change("httpx", "0.27.2", "0.28.1", "requirements.txt")]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, {"svc/requirements.txt": "httpx==0.27.2\n", "svc/uv.lock": UV_LOCK, "README": "x"})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_a_revision_is_read_without_checking_it_out(repo):
    """A pull request's base and head: base from git, head from the working tree, in a subdirectory project."""
    svc = repo / "svc"
    (svc / "requirements.txt").write_text("httpx==0.28.1\n")
    (svc / "uv.lock").write_text(UV_LOCK.replace('version = "0.27.2"', 'version = "0.28.1"'))
    base, head = read_pins(svc, "HEAD"), read_pins(svc)
    assert base.by_file == {
        "uv.lock": {"httpx": "0.27.2", "pydantic-core": "2.20.1"},
        "requirements.txt": {"httpx": "0.27.2"},
    }
    assert changes(base, head) == [Change("httpx", "0.27.2", "0.28.1", "requirements.txt")]


def test_an_unknown_revision_is_an_error(repo):
    with pytest.raises(OSError):
        read_pins(repo, "no-such-rev")


def test_requirements_in_subdirectories_are_read_at_any_depth(tmp_path):
    """DT-CORE-71: Dependabot updates `python-backend/requirements.txt` with no directory in the title, and the Action
    runs at the root. Virtual environments, build output and hidden directories are left out."""
    _write(
        tmp_path,
        {
            "python-backend/requirements.txt": "openai==1.40.0\n",
            "tests/requirements-sdk.txt": "pytest==8.0.0\n",
            "services/api/requirements/prod.txt": "httpx==0.27.2\n",
            "docs/notes.txt": "httpx==0.1.0\n",  # neither named nor placed as a requirements file
            ".venv/lib/requirements.txt": "httpx==0.1.0\n",
            "venv/requirements.txt": "httpx==0.1.0\n",
            "node_modules/x/requirements.txt": "httpx==0.1.0\n",
            ".github/requirements.txt": "httpx==0.1.0\n",
        },
    )
    assert read_pins(tmp_path).by_file == {
        "python-backend/requirements.txt": {"openai": "1.40.0"},
        "tests/requirements-sdk.txt": {"pytest": "8.0.0"},
        "services/api/requirements/prod.txt": {"httpx": "0.27.2"},
    }


def test_a_subdirectory_requirements_update_is_read_from_the_repository_root(repo):
    """The same at a git revision: the root project sees the change to `svc/requirements.txt`."""
    (repo / "svc" / "requirements.txt").write_text("httpx==0.28.1\n")
    base, head = read_pins(repo, "HEAD"), read_pins(repo)
    assert base.by_file["svc/requirements.txt"] == {"httpx": "0.27.2"}
    assert changes(base, head) == [Change("httpx", "0.27.2", "0.28.1", "svc/requirements.txt")]


def test_lockfiles_in_subdirectories_are_read_at_any_depth(tmp_path):
    """DT-CORE-74: Renovate updates `llm/uv.lock` and `mcp/client/uv.lock` in one pull request, and the Action runs at
    the root. The root lockfile comes first; each lockfile's own project is no dependency."""
    _write(
        tmp_path,
        {
            "uv.lock": UV_LOCK,
            "llm/uv.lock": UV_LOCK.replace('"0.27.2"', '"0.28.1"').replace("my-project", "llm"),
            "mcp/client/poetry.lock": POETRY_LOCK,
            ".venv/uv.lock": UV_LOCK,
            "node_modules/x/poetry.lock": POETRY_LOCK,
        },
    )
    pins = read_pins(tmp_path)
    assert list(pins.by_file) == ["uv.lock", "llm/uv.lock", "mcp/client/poetry.lock"]
    assert pins.versions("httpx") == {"uv.lock": "0.27.2", "llm/uv.lock": "0.28.1"}
    assert pins.by_file["mcp/client/poetry.lock"] == {"numpy": "1.26.4"}


def test_a_subdirectory_lockfile_update_is_read_from_the_repository_root(repo):
    (repo / "svc" / "uv.lock").write_text(
        UV_LOCK.replace('version = "0.27.2"', 'version = "0.28.1"').replace('"0.1.0"', '"0.2.0"')
    )
    assert changes(read_pins(repo, "HEAD"), read_pins(repo)) == [
        Change("httpx", "0.27.2", "0.28.1", "svc/uv.lock")
    ]


def test_a_byte_order_mark_doesnt_hide_a_pin(tmp_path):
    """DT-CORE-81: Notepad saves `requirements.txt` with a BOM, which made its first line an invalid requirement."""
    bom = b"\xef\xbb\xbf"
    (tmp_path / "requirements.txt").write_bytes(bom + b"httpx==0.27.2\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "uv.lock").write_bytes(bom + UV_LOCK.encode())
    (tmp_path / "pyproject.toml").write_bytes(bom + b'[project]\nname = "My_Project"\n')
    pins = read_pins(tmp_path)
    assert pins.problems == []
    assert pins.by_file["requirements.txt"] == {"httpx": "0.27.2"} and "httpx" in pins.by_file["sub/uv.lock"]
    assert project_name(tmp_path) == "my-project"


def test_a_pyproject_with_the_wrong_shapes_is_reported_not_a_crash(tmp_path):
    """DT-CORE-80: `project = "x"` raised AttributeError out of `check-upgrade`, which exited 1 (a break)."""
    for text in ('project = "x"\n', "[project]\ndependencies = 3\n", 'dependency-groups = "dev"\n'):
        _write(tmp_path, {"pyproject.toml": text})
        pins = read_pins(tmp_path)
        assert "pyproject.toml" not in pins.by_file and pins.problems[0].startswith("pyproject.toml: "), text
    assert project_name(tmp_path) is None


def test_requirements_keeps_first_pin_when_package_is_listed_twice(tmp_path):
    _write(
        tmp_path,
        {
            "requirements.txt": 'numpy==2.1.0; python_version >= "3.9"\nnumpy==1.26.4; python_version < "3.9"\n',
        },
    )
    base = read_pins(tmp_path)
    assert base.by_file == {"requirements.txt": {"numpy": "2.1.0"}}

    _write(
        tmp_path,
        {
            "requirements.txt": 'numpy==2.2.0; python_version >= "3.9"\nnumpy==1.26.4; python_version < "3.9"\n',
        },
    )
    head = read_pins(tmp_path)
    assert changes(base, head) == [Change("numpy", "2.1.0", "2.2.0", "requirements.txt")]
