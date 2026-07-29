import json
import subprocess
from pathlib import Path

import pytest

from app.services.dependencies import detect_manifest, Manifest, _install_npm


def test_detect_manifest_prefers_requirements_txt_over_pyproject(tmp_path):
    (tmp_path / "requirements.txt").write_text("six\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')

    manifest = detect_manifest(tmp_path)

    assert manifest.kind == "pip"
    assert manifest.path.name == "requirements.txt"


def test_detect_manifest_falls_back_to_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')

    manifest = detect_manifest(tmp_path)

    assert manifest.kind == "pip"
    assert manifest.path.name == "pyproject.toml"


def test_detect_manifest_falls_back_to_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "version": "1.0.0"}))

    manifest = detect_manifest(tmp_path)

    assert manifest.kind == "npm"
    assert manifest.path.name == "package.json"


def test_detect_manifest_returns_none_when_nothing_present(tmp_path):
    assert detect_manifest(tmp_path) is None


@pytest.mark.skipif(
    subprocess.run(["which", "npm"], capture_output=True).returncode != 0,
    reason="npm not installed",
)
def test_install_dependencies_npm_succeeds(tmp_path):
    # Create a minimal valid package.json
    package_json = tmp_path / "package.json"
    package_json.write_text(json.dumps({"name": "test-app", "version": "1.0.0"}))

    manifest = Manifest(kind="npm", path=package_json)
    result = _install_npm(tmp_path)

    assert result.ok is True
    assert result.venv_bin is None


@pytest.mark.skipif(
    subprocess.run(["which", "npm"], capture_output=True).returncode != 0,
    reason="npm not installed",
)
def test_install_dependencies_npm_reports_failure(tmp_path):
    # Create invalid JSON in package.json
    package_json = tmp_path / "package.json"
    package_json.write_text("{invalid json}")

    result = _install_npm(tmp_path)

    assert result.ok is False
    assert result.venv_bin is None
