# tests/test_tests_runner.py
from pathlib import Path

from app.services.tests_runner import run_tests


def test_run_tests_passes_on_success(tmp_path: Path):
    result = run_tests(tmp_path, "true")
    assert result.passed is True


def test_run_tests_fails_on_nonzero_exit(tmp_path: Path):
    result = run_tests(tmp_path, "false")
    assert result.passed is False


def test_run_tests_captures_output(tmp_path: Path):
    result = run_tests(tmp_path, "echo hello-from-test")
    assert "hello-from-test" in result.output


def test_run_tests_handles_timeout(tmp_path: Path):
    result = run_tests(tmp_path, "sleep 5", timeout=1)
    assert result.passed is False
    assert "timed out" in result.output
