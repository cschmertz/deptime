"""The Claude Code hint hook: stdlib-only script, exercised as a subprocess exactly like Claude Code runs it."""

import json
import subprocess
import sys
from pathlib import Path

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
