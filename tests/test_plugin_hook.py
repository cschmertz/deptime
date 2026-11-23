"""The Claude Code hint hook: stdlib-only script, exercised as a subprocess exactly like Claude Code runs it."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "integrations/claude-code/scripts/suggest_deptime.py"


def _run(payload: dict, data_dir: Path | None = None) -> dict | None:
    env = {"PATH": "/usr/bin:/bin"}
    if data_dir:
        env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(out.stdout) if out.stdout.strip() else None


def _payload(stderr: str, event: str = "PostToolUseFailure") -> dict:
    return {
        "session_id": "s1",
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": "python app.py"},
        "tool_response": {"stdout": "", "stderr": stderr, "exit_code": 1},
    }


def test_suggests_on_version_change_error(tmp_path):
    tb = "Traceback (most recent call last):\n  ...\nTypeError: Client.__init__() got an unexpected keyword argument 'proxies'\n"
    out = _run(_payload(tb), tmp_path)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "explain_error" in out["hookSpecificOutput"]["additionalContext"]
    assert _run(_payload(tb), tmp_path) is None  # same error, same session: suggested once


def test_ignores_unrelated_output_and_top_level_missing_module(tmp_path):
    assert _run(_payload("all tests passed", "PostToolUse"), tmp_path) is None
    assert _run(_payload("ModuleNotFoundError: No module named 'requests'"), tmp_path) is None


def test_never_crashes_on_garbage():
    out = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == ""


def test_suggests_on_python_313_did_you_mean_suffix(tmp_path):
    # P-01: CPython 3.13+ appends a suggestion to near-miss kwargs, which is what renamed parameters look like.
    tb = "TypeError: Client.__init__() got an unexpected keyword argument 'proxys'. Did you mean 'proxy'?\n"
    out = _run(_payload(tb), tmp_path)
    assert "explain_error" in out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
@pytest.mark.parametrize("has_python", [True, False])
def test_registered_hook_dispatch(tmp_path, event, has_python):
    # Test the shipped registrations, including plugin paths with shell metacharacters.
    plugin = tmp_path / "plugin's $root"
    shutil.copytree(SCRIPT.parents[1], plugin)
    hook = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"][event][0]
    assert hook["matcher"] == "Bash"
    command = hook["hooks"][0]
    assert command["shell"] == "bash" and "args" not in command

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    search = [bin_dir]
    env = {"CLAUDE_PLUGIN_ROOT": plugin.as_posix()}
    if sys.platform == "win32":
        shell = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe"
        if not shell.is_file():
            pytest.skip("Git Bash is required to exercise the Windows command hook")
        env["SystemRoot"] = os.environ["SYSTEMROOT"]
        aliases = bin_dir / "WindowsApps"
        aliases.mkdir()
        # Inert executables stand in for Store aliases. Their execution would fail the assertions below.
        for name in ("python.exe", "python3.exe"):
            shutil.copyfile(Path(env["SystemRoot"]) / "System32/where.exe", aliases / name)
        search.insert(0, aliases)
        if has_python:
            interpreter_dir = Path(sys._base_executable).parent
            assert (interpreter_dir / "python.exe").is_file()
            search.append(interpreter_dir)
    else:
        shell = Path("/bin/sh")
        if has_python:
            (bin_dir / "python3").symlink_to(sys.executable)
    env["PATH"] = os.pathsep.join(map(str, search))

    error = "ImportError: cannot import name 'Foo' from 'httpx'"
    for message in ("hello", error):
        result = subprocess.run(
            [str(shell), "-c", command["command"]],
            input=json.dumps(_payload(message, event)),
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
            check=True,
            timeout=10,
        )
        assert result.stderr == ""
        if has_python and message == error:
            output = json.loads(result.stdout)["hookSpecificOutput"]
            assert output["hookEventName"] == event
            assert "explain_error" in output["additionalContext"]
        else:
            assert result.stdout == ""
