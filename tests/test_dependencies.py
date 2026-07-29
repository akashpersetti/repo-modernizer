import json
from pathlib import Path

from app.services.dependencies import detect_manifest


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
