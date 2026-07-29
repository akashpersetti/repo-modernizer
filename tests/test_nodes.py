import json
from pathlib import Path

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps, _is_test_file, _is_vendor_dir, _strip_code_fence


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


def test_is_test_file_excludes_js_test_conventions():
    assert _is_test_file(Path("src/components/App.test.js")) is True
    assert _is_test_file(Path("src/components/App.spec.jsx")) is True
    assert _is_test_file(Path("__tests__/App.js")) is True


def test_is_test_file_allows_normal_js_source():
    assert _is_test_file(Path("src/components/App.js")) is False


def test_is_vendor_dir_excludes_common_dependency_directories():
    assert _is_vendor_dir(Path("node_modules/react/index.js")) is True
    assert _is_vendor_dir(Path("venv/lib/site-packages/x.py")) is True
    assert _is_vendor_dir(Path(".venv/lib/x.py")) is True
    assert _is_vendor_dir(Path("dist/bundle.js")) is True


def test_is_vendor_dir_allows_normal_source():
    assert _is_vendor_dir(Path("src/components/App.js")) is False


class _FakeProviderRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        text = self.responses[self.calls]
        self.calls += 1
        return text, 100, 50, "bedrock-primary"


def test_plan_node_respects_configured_file_extensions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "webapp.py").write_text("x = 1\n")
    (repo / "App.js").write_text("console.log(1);\n")
    (repo / "App.test.js").write_text("test('x', () => {});\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("module.exports = {};\n")

    fake = _FakeProviderRouter([json.dumps([])])
    deps = NodeDeps(
        providers=fake, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "ext-test"}}
    state = {
        "task_id": "ext-test", "repo_path": str(repo), "goal": "g", "test_command": "true",
        "plan": [], "files": {}, "cursor": 0, "cost_used_usd": 0.0, "trace": [],
        "file_extensions": [".js", ".jsx"],
    }

    graph.invoke(state, config=config)

    plan_prompt = fake.prompts[0]
    assert "App.js" in plan_prompt
    assert "webapp.py" not in plan_prompt
    assert "App.test.js" not in plan_prompt
    assert "node_modules" not in plan_prompt
