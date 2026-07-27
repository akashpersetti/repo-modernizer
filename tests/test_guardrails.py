# tests/test_guardrails.py
from app.agent.guardrails import is_forbidden, validate_diff


def _diff(target: str = "app.py", lines: int = 2, delete: bool = False) -> str:
    if delete:
        return f"--- a/{target}\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    body = "".join(f"+line{i}\n-oldline{i}\n" for i in range(lines // 2))
    return f"--- a/{target}\n+++ b/{target}\n@@ -1,1 +1,1 @@\n{body}"


def test_is_forbidden_matches_dir_prefix():
    assert is_forbidden(".github/workflows/ci.yml", [".github/", "migrations/"]) is True


def test_is_forbidden_matches_glob():
    assert is_forbidden("poetry.lock", ["*.lock"]) is True


def test_is_forbidden_allows_normal_path():
    assert is_forbidden("app/main.py", [".github/", "migrations/", "*.lock"]) is False


def test_validate_diff_rejects_delete():
    ok, reason = validate_diff(_diff(delete=True), "app.py", [], max_lines=400)
    assert ok is False
    assert "delete" in reason


def test_validate_diff_rejects_forbidden_path():
    ok, reason = validate_diff(_diff(target="migrations/0001.py"), "migrations/0001.py", ["migrations/"], max_lines=400)
    assert ok is False
    assert "forbidden" in reason


def test_validate_diff_rejects_wrong_target():
    ok, reason = validate_diff(_diff(target="other.py"), "app.py", [], max_lines=400)
    assert ok is False
    assert "outside target file" in reason


def test_validate_diff_rejects_oversize():
    ok, reason = validate_diff(_diff(lines=1000), "app.py", [], max_lines=10)
    assert ok is False
    assert "exceeds cap" in reason


def test_validate_diff_accepts_clean_diff():
    ok, reason = validate_diff(_diff(), "app.py", [], max_lines=400)
    assert ok is True
    assert reason is None
