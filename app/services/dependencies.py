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
