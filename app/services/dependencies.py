import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

INSTALL_TIMEOUT_SECONDS = 300


@dataclass
class Manifest:
    kind: Literal["pip", "npm"]
    path: Path


@dataclass
class InstallResult:
    ok: bool
    venv_bin: Optional[Path]
    output: str


def detect_manifest(root: Path) -> Optional[Manifest]:
    requirements = root / "requirements.txt"
    if requirements.exists():
        return Manifest(kind="pip", path=requirements)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        return Manifest(kind="pip", path=pyproject)
    package_json = root / "package.json"
    if package_json.exists():
        return Manifest(kind="npm", path=package_json)
    return None


def _install_npm(root: Path) -> InstallResult:
    """Install npm dependencies in the target repo's directory.

    Runs 'npm install' in the root directory, which installs into
    node_modules there, self-isolating the installation.

    Args:
        root: Root directory containing package.json

    Returns:
        InstallResult with ok=True and venv_bin=None on success,
        ok=False and venv_bin=None on failure.
    """
    try:
        result = subprocess.run(
            ["npm", "install"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return InstallResult(ok=True, venv_bin=None, output=result.stdout)
        else:
            return InstallResult(ok=False, venv_bin=None, output=result.stderr)
    except subprocess.TimeoutExpired:
        return InstallResult(ok=False, venv_bin=None, output="npm install timed out")
    except Exception as e:
        return InstallResult(ok=False, venv_bin=None, output=str(e))


def install_dependencies(root: Path, manifest: Manifest) -> InstallResult:
    if manifest.kind == "npm":
        return _install_npm(root)
    return _install_pip(root, manifest)


def _install_pip(root: Path, manifest: Manifest) -> InstallResult:
    venv_dir = root / ".repomod-venv"
    try:
        create = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return InstallResult(ok=False, venv_bin=None, output=f"venv creation timed out: {exc}")
    if create.returncode != 0:
        return InstallResult(ok=False, venv_bin=None, output=create.stdout + create.stderr)

    venv_bin = venv_dir / "bin"
    pip = venv_bin / "pip"
    if manifest.path.name == "requirements.txt":
        args = [str(pip), "install", "-r", str(manifest.path)]
    else:
        args = [str(pip), "install", str(root)]
    try:
        install = subprocess.run(
            args, cwd=root, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return InstallResult(ok=False, venv_bin=None, output=f"pip install timed out: {exc}")
    if install.returncode != 0:
        return InstallResult(ok=False, venv_bin=None, output=install.stdout + install.stderr)
    return InstallResult(ok=True, venv_bin=venv_bin, output=install.stdout + install.stderr)


