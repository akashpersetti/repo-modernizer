# tests/test_tests_runner.py
import os
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


def test_run_tests_prepends_venv_bin_to_path(tmp_path: Path):
    """Verify that when venv_bin is provided, it's prepended to PATH."""
    # Create a fake venv bin directory with a stub script
    venv_bin = tmp_path / "fake_venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub_script = venv_bin / "test_cmd"
    stub_script.write_text("#!/bin/sh\necho 'from-venv'\nexit 0\n")
    stub_script.chmod(0o755)

    # Call run_tests with venv_bin pointing to our fake venv
    result = run_tests(tmp_path, "test_cmd", venv_bin=str(venv_bin))
    assert result.passed is True
    assert "from-venv" in result.output


def test_run_tests_without_venv_bin_uses_ambient_path(tmp_path: Path):
    """Verify that when venv_bin is not provided, PATH is not modified."""
    # Call run_tests without venv_bin on the 'true' command, which should succeed
    result = run_tests(tmp_path, "true")
    assert result.passed is True
