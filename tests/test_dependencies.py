import json
import subprocess
from pathlib import Path

import pytest

from app.services.dependencies import detect_manifest, Manifest, install_dependencies, _install_npm


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


def test_install_dependencies_pip_requirements_txt_succeeds(tmp_path):
    (tmp_path / "requirements.txt").write_text("iniconfig==2.0.0\n")
    manifest = Manifest(kind="pip", path=tmp_path / "requirements.txt")

    result = install_dependencies(tmp_path, manifest)

    assert result.ok is True
    assert result.venv_bin is not None
    check = subprocess.run(
        [str(result.venv_bin / "python"), "-c", "import iniconfig; print(iniconfig.__name__)"],
        capture_output=True, text=True,
    )
    assert check.returncode == 0
    assert check.stdout.strip() == "iniconfig"


def test_install_dependencies_pip_requirements_txt_reports_failure(tmp_path):
    (tmp_path / "requirements.txt").write_text("this-package-definitely-does-not-exist-xyz123==0.0.0\n")
    manifest = Manifest(kind="pip", path=tmp_path / "requirements.txt")

    result = install_dependencies(tmp_path, manifest)

    assert result.ok is False
    assert result.venv_bin is None


def test_install_dependencies_pip_pyproject_calls_pip_install_dot(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.0.1"\n')
    manifest = Manifest(kind="pip", path=tmp_path / "pyproject.toml")

    captured_args = []
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        captured_args.append(args)
        if args[0] == str((tmp_path / ".repomod-venv" / "bin" / "pip")):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    install_dependencies(tmp_path, manifest)

    pip_call = next(a for a in captured_args if str(tmp_path / ".repomod-venv" / "bin" / "pip") in a[0])
    assert pip_call == [str(tmp_path / ".repomod-venv" / "bin" / "pip"), "install", str(tmp_path)]


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
