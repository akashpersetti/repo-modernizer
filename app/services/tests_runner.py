import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TestRunResult:
    passed: bool
    output: str


def run_tests(
    workspace_root: Path,
    test_command: str,
    venv_bin: Optional[str] = None,
    timeout: int = 60,
) -> TestRunResult:
    env = os.environ.copy()
    if venv_bin:
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            shlex.split(test_command),
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return TestRunResult(passed=False, output=f"test command timed out after {timeout}s: {exc}")
    return TestRunResult(passed=proc.returncode == 0, output=proc.stdout + proc.stderr)
