# tests/test_diffs.py
import subprocess
from pathlib import Path

import pytest

from app.services.diffs import apply_diff, make_diff, parse_unified_diff


def _make_diff(before: str, after: str, path: str = "app.py") -> str:
    return make_diff(before, after, path)


def test_make_diff_produces_git_apply_compatible_output(tmp_path: Path):
    repo = tmp_path
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    diff = make_diff("x = 1\n", "x = 2\n", "app.py")
    apply_diff(diff, repo)

    assert (repo / "app.py").read_text() == "x = 2\n"


def test_make_diff_no_change_produces_empty_diff():
    assert make_diff("x = 1\n", "x = 1\n", "app.py") == ""


def test_make_diff_applies_when_after_missing_trailing_newline(tmp_path: Path):
    # LLM-generated file content routinely omits the final newline; make_diff must
    # still produce a diff `git apply` accepts against a source that has one.
    repo = tmp_path
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    diff = make_diff("x = 1\n", "x = 2", "app.py")  # note: no trailing newline
    apply_diff(diff, repo)

    assert (repo / "app.py").read_text() == "x = 2\n"


def test_parse_unified_diff_basic():
    diff = _make_diff("x = 1\n", "x = 2\n")
    parsed = parse_unified_diff(diff)
    assert parsed.target_paths == ["app.py"]
    assert parsed.deletes is False
    assert parsed.lines_changed == 2  # one removed, one added


def test_parse_unified_diff_detects_delete():
    diff = "--- a/app.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    parsed = parse_unified_diff(diff)
    assert parsed.deletes is True


def test_apply_diff_applies_to_workspace(tmp_path: Path):
    repo = tmp_path
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    diff = _make_diff("x = 1\n", "x = 2\n")

    apply_diff(diff, repo)

    assert (repo / "app.py").read_text() == "x = 2\n"


def test_apply_diff_raises_on_bad_diff(tmp_path: Path):
    repo = tmp_path
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    with pytest.raises(RuntimeError):
        apply_diff("not a real diff", repo)
