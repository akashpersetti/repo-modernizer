import difflib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedDiff:
    target_paths: list[str]
    deletes: bool
    lines_changed: int


_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$", re.MULTILINE)
_DELETE_RE = re.compile(r"^\+\+\+ /dev/null$", re.MULTILINE)


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    target_paths = [p.strip() for p in _FILE_HEADER_RE.findall(diff_text) if p.strip() != "/dev/null"]
    deletes = bool(_DELETE_RE.search(diff_text))
    lines_changed = sum(
        1
        for line in diff_text.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    )
    return ParsedDiff(target_paths=target_paths, deletes=deletes, lines_changed=lines_changed)


def make_diff(before: str, after: str, path: str) -> str:
    """Build a unified diff from known-good before/after text for one file.

    Used instead of asking an LLM to hand-write a diff: the LLM returns the full
    new file content, and we compute the diff ourselves so context lines always
    match the real on-disk content byte-for-byte, guaranteeing `git apply` succeeds.

    Callers must ensure `before` reflects the real on-disk file byte-for-byte --
    including its actual trailing-newline status. `git apply` matches context
    against real file bytes; claiming a trailing newline the real file doesn't
    have (or vice versa) makes the last hunk mismatch and the whole patch gets
    rejected. difflib.unified_diff also never emits the POSIX no-newline-at-EOF
    marker a completely honest diff would need in that case, so
    `nodes.py` normalizes the on-disk file to genuinely end with "\n" before
    ever calling this — the correct fix is upstream of here, not a marker
    hand-rolled into this function's output.
    """
    if after and not after.endswith("\n"):
        after += "\n"
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))


def apply_diff(diff_text: str, workspace_root: Path) -> None:
    result = subprocess.run(
        ["git", "apply", "-"],
        cwd=workspace_root,
        input=diff_text,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git apply failed: {result.stderr}")
