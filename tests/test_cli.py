# tests/test_cli.py
from unittest.mock import MagicMock, patch

from app.cli import run


@patch("app.cli.boto3")
@patch("app.cli.build_graph")
def test_run_wires_graph_and_invokes_once(mock_build_graph, mock_boto3, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {"cost_used_usd": 0.05, "files": {}}
    mock_build_graph.return_value = mock_graph

    run(str(repo), "bump x", "true")

    mock_graph.invoke.assert_called_once()
    initial_state = mock_graph.invoke.call_args[0][0]
    assert initial_state["goal"] == "bump x"
    assert initial_state["test_command"] == "true"


@patch("app.cli.boto3")
@patch("app.cli.build_graph")
@patch("builtins.input", return_value="approve")
def test_run_prompts_and_resumes_on_interrupt(mock_input, mock_build_graph, mock_boto3, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    mock_graph = MagicMock()
    mock_graph.invoke.side_effect = [
        {"__interrupt__": [MagicMock(value={"path": "app.py", "diff": "diff text", "risk_score": 0.9})]},
        {"cost_used_usd": 0.05, "files": {}},
    ]
    mock_build_graph.return_value = mock_graph

    run(str(repo), "bump x", "true")

    assert mock_graph.invoke.call_count == 2
    mock_input.assert_called()
