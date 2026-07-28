from app.agent.state import GraphState


def test_graph_state_has_repo_context_fields():
    state: GraphState = {
        "task_id": "t", "repo_path": "/tmp/x", "goal": "g", "test_command": "true",
        "plan": [], "files": {}, "cursor": 0, "cost_used_usd": 0.0, "trace": [],
        "repo_url": "https://github.com/x/y", "branch": "repomodernizer/t", "base_branch": "main",
    }
    assert state["repo_url"] == "https://github.com/x/y"
