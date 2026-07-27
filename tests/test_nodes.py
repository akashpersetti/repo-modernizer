from pathlib import Path

from app.agent.nodes import _is_test_file, _strip_code_fence


def test_strip_code_fence_removes_json_fence():
    text = '```json\n[{"path": "a.py"}]\n```'
    assert _strip_code_fence(text) == '[{"path": "a.py"}]\n'


def test_strip_code_fence_removes_plain_fence():
    text = "```\ndiff --git a/x b/x\n```"
    assert _strip_code_fence(text) == "diff --git a/x b/x\n"


def test_strip_code_fence_passes_through_unfenced_text():
    text = '[{"path": "a.py"}]'
    assert _strip_code_fence(text) == '[{"path": "a.py"}]'


def test_is_test_file_excludes_conftest():
    assert _is_test_file(Path("conftest.py")) is True
    assert _is_test_file(Path("sub/conftest.py")) is True


def test_is_test_file_excludes_tests_dir_and_test_prefix():
    assert _is_test_file(Path("tests/test_app.py")) is True
    assert _is_test_file(Path("test_app.py")) is True
    assert _is_test_file(Path("app_test.py")) is True


def test_is_test_file_allows_normal_source():
    assert _is_test_file(Path("webapp.py")) is False
