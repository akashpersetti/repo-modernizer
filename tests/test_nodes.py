from app.agent.nodes import _strip_code_fence


def test_strip_code_fence_removes_json_fence():
    text = '```json\n[{"path": "a.py"}]\n```'
    assert _strip_code_fence(text) == '[{"path": "a.py"}]\n'


def test_strip_code_fence_removes_plain_fence():
    text = "```\ndiff --git a/x b/x\n```"
    assert _strip_code_fence(text) == "diff --git a/x b/x\n"


def test_strip_code_fence_passes_through_unfenced_text():
    text = '[{"path": "a.py"}]'
    assert _strip_code_fence(text) == '[{"path": "a.py"}]'
