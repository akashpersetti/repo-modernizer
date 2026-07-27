import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestRunResult:
    passed: bool
    output: str


def run_tests(workspace_root: Path, test_command: str, timeout: int = 60) -> TestRunResult:
    try:
        proc = subprocess.run(
            shlex.split(test_command),
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return TestRunResult(passed=False, output=f"test command timed out after {timeout}s: {exc}")
    return TestRunResult(passed=proc.returncode == 0, output=proc.stdout + proc.stderr)
