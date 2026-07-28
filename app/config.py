from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"
    aws_region: str = "us-east-1"
    bedrock_model_primary: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    bedrock_model_fallback: str = "anthropic.claude-3-haiku-20240307-v1:0"
    max_task_cost_usd: float = 2.00
    max_file_retries: int = 2
    risk_approval_threshold: float = 0.6
    forbidden_paths: str = ".github/,migrations/,*.lock"
    max_diff_lines: int = 400
    estimated_cost_per_file_usd: float = 0.05
    github_app_token: str = ""
    github_default_base_branch: str = "main"
    ddb_table_checkpoints: str = "repomod-checkpoints"

    def forbidden_paths_list(self) -> list[str]:
        return [p.strip() for p in self.forbidden_paths.split(",") if p.strip()]
