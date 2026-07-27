import fnmatch
from pathlib import Path

from app.services.diffs import parse_unified_diff


def is_forbidden(path: str, forbidden_patterns: list[str]) -> bool:
    for pattern in forbidden_patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern):
            return True
    return False


def validate_diff(
    diff_text: str, target_path: str, forbidden_paths: list[str], max_lines: int
) -> tuple[bool, str | None]:
    parsed = parse_unified_diff(diff_text)
    if parsed.deletes:
        return False, "diff deletes a file"
    if is_forbidden(target_path, forbidden_paths):
        return False, f"target path '{target_path}' is forbidden"
    if any(p != target_path for p in parsed.target_paths):
        return False, f"diff writes outside target file '{target_path}'"
    if parsed.lines_changed > max_lines:
        return False, f"diff changes {parsed.lines_changed} lines, exceeds cap {max_lines}"
    return True, None
