# tests/test_config.py
from app.config import Settings


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_PRIMARY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.max_task_cost_usd == 2.00
    assert settings.max_file_retries == 2
    assert settings.risk_approval_threshold == 0.6
    assert settings.forbidden_paths_list() == [".github/", "migrations/", "*.lock"]


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("MAX_TASK_COST_USD", "5.00")
    settings = Settings(_env_file=None)
    assert settings.max_task_cost_usd == 5.00


def test_settings_has_github_and_checkpoint_defaults(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_TOKEN", raising=False)
    settings = Settings(_env_file=None)
    assert settings.github_default_base_branch == "main"
    assert settings.ddb_table_checkpoints == "repomod-checkpoints"
    assert settings.github_app_token == ""
