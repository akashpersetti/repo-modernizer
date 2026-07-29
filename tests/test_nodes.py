import json
from pathlib import Path

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps, install_deps_node, _is_test_file, _is_vendor_dir, _strip_code_fence


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
        "venv_bin": None,
        "dependency_install_failed": False,
        "repo_url": "https://github.com/example/repo",
        "branch": "main",
        "base_branch": "main",
    }

    graph.invoke(state, config=config)

    plan_prompt = fake.prompts[0]
    assert "App.js" in plan_prompt
    assert "webapp.py" not in plan_prompt
    assert "App.test.js" not in plan_prompt
    assert "node_modules" not in plan_prompt


def test_migrate_file_node_applies_diff_when_source_lacks_trailing_newline(tmp_path):
    # Found live against a real JS repo: a source file with no trailing newline
    # (common in hand-written JS, never exercised by the Python fixtures, which
    # all happened to end with one) made git apply reject the diff outright --
    # difflib claimed/implied a newline the real file didn't have. migrate_file_node
    # must normalize the file on disk before diffing so this can't happen.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.js").write_bytes(b"console.log(1);")  # deliberately no trailing newline

    fake = _FakeProviderRouter([
        json.dumps([{"path": "app.js", "rationale": "t", "risk_score": 0.1}]),
        "console.log(2);",  # LLM output also lacks a trailing newline, same as reality
    ])
    deps = NodeDeps(
        providers=fake, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "no-newline-test"}}
    state = {
        "task_id": "no-newline-test", "repo_path": str(repo), "goal": "g", "test_command": "true",
        "plan": [], "files": {}, "cursor": 0, "cost_used_usd": 0.0, "trace": [],
        "file_extensions": [".js"],
        "venv_bin": None,
        "dependency_install_failed": False,
        "repo_url": "https://github.com/example/repo",
        "branch": "main",
        "base_branch": "main",
    }

    result = graph.invoke(state, config=config)

    assert result["files"]["app.js"]["status"] == "migrated"
    assert (repo / "app.js").read_text() == "console.log(2);\n"


def test_install_deps_node_skips_when_no_manifest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    deps = NodeDeps(
        providers=None, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    state = {
        "repo_path": str(repo),
        "trace": [{"node": "ingest", "note": "workspace initialized"}],
    }

    result = install_deps_node(state, deps)

    assert result["dependency_install_failed"] is False
    assert result["venv_bin"] is None
    assert len(result["trace"]) == 2
    assert result["trace"][1]["node"] == "install_deps"
    assert result["trace"][1]["note"] == "no dependency manifest found, skipping install"


def test_install_deps_node_reports_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("invalid-package-name-that-does-not-exist==9.9.9\n")

    deps = NodeDeps(
        providers=None, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    state = {
        "repo_path": str(repo),
        "trace": [{"node": "ingest", "note": "workspace initialized"}],
    }

    result = install_deps_node(state, deps)

    assert result["dependency_install_failed"] is True
    assert result["venv_bin"] is None
    assert len(result["trace"]) == 2
    assert result["trace"][1]["node"] == "install_deps"
    assert "install failed:" in result["trace"][1]["note"]


def test_finalize_node_reports_dependency_install_failure(tmp_path):
    from app.agent.nodes import finalize_node

    repo = tmp_path / "repo"
    repo.mkdir()
    deps = NodeDeps(
        providers=None, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    state = {
        "repo_path": str(repo), "trace": [], "files": {}, "cost_used_usd": 0.0,
        "dependency_install_failed": True,
    }

    finalize_node(state, deps)

    summary = (tmp_path / "summary.md").read_text()
    assert "Dependency installation failed" in summary
