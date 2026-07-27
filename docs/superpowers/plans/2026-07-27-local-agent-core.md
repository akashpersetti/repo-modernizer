# Local Agent Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local-only LangGraph migration agent (sub-project 1 of RepoModernizer) — a CLI that migrates a repo file-by-file toward a stated goal, gated by guardrails/risk/budget, with human-in-the-loop approval on risky diffs. No AWS infra, no FastAPI, no GitHub integration.

**Architecture:** A `StateGraph` with four nodes (`ingest → plan → migrate_file(loop) → finalize`) checkpointed in-memory (`MemorySaver`). `migrate_file` calls an LLM for a unified diff, validates it against guardrails, scores risk and calls `interrupt()` above threshold, applies the diff via `git apply`, runs the repo's test command, and retries on failure. A CLI (`repomod run`) drives the graph and prompts on the terminal for interrupt approval.

**Tech Stack:** Python 3.12, `uv`, `langgraph`, `boto3` (Bedrock Runtime), `pydantic-settings`, `pytest`.

## Global Constraints

- `MAX_TASK_COST_USD` default `2.00` (spec §5) — hard stop, remaining files marked `skipped`.
- `MAX_FILE_RETRIES` default `2` (spec §5) — bounded retry on guardrail rejection or test failure before a file is marked `failed`.
- `RISK_APPROVAL_THRESHOLD` default `0.6` (spec §5) — `>=` this score forces `interrupt()`.
- `FORBIDDEN_PATHS` default `.github/,migrations/,*.lock` (spec §5) — diffs touching these paths are rejected outright, no retry benefit (guardrail, not risk).
- Bedrock primary + a second Bedrock model as fallback (decided in brainstorming) — both configured via env, no non-AWS API key in this sub-project.
- No DynamoDB, no FastAPI, no GitHub PR integration, no Terraform in this sub-project (deferred to later sub-projects per `docs/superpowers/specs/2026-07-27-local-agent-core-design.md`).
- Checkpointer is LangGraph's in-memory `MemorySaver`; interrupt approval is a terminal `input()` prompt in the CLI.

---

### Task 1: Project scaffolding + config

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `app.config.Settings` (pydantic-settings `BaseSettings` subclass) with fields `env`, `log_level`, `aws_region`, `bedrock_model_primary`, `bedrock_model_fallback`, `max_task_cost_usd`, `max_file_retries`, `risk_approval_threshold`, `forbidden_paths` (comma-joined string), `max_diff_lines`, `estimated_cost_per_file_usd`; method `forbidden_paths_list() -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'` (package doesn't exist yet).

- [ ] **Step 3: Write scaffolding and implementation**

```toml
# pyproject.toml
[project]
name = "repomodernizer"
version = "0.1.0"
description = "Autonomous repository modernization agent — local agent core"
requires-python = ">=3.12"
dependencies = [
    "langgraph>=0.2",
    "boto3>=1.34",
    "pydantic-settings>=2.4",
]

[project.scripts]
repomod = "app.cli:main"

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "flask>=3.0",
    "fastapi>=0.115",
    "httpx>=0.27",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]
```

```
# .env.example
ENV=local
LOG_LEVEL=INFO
AWS_REGION=us-east-1
BEDROCK_MODEL_PRIMARY=anthropic.claude-3-5-sonnet-20241022-v2:0
BEDROCK_MODEL_FALLBACK=anthropic.claude-3-haiku-20240307-v1:0
MAX_TASK_COST_USD=2.00
MAX_FILE_RETRIES=2
RISK_APPROVAL_THRESHOLD=0.6
FORBIDDEN_PATHS=.github/,migrations/,*.lock
MAX_DIFF_LINES=400
ESTIMATED_COST_PER_FILE_USD=0.05
```

```
# .gitignore
.env
.venv/
__pycache__/
*.pyc
.pytest_cache/
runs/
```

```python
# app/__init__.py
```

```python
# app/config.py
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

    def forbidden_paths_list(self) -> list[str]:
        return [p.strip() for p in self.forbidden_paths.split(",") if p.strip()]
```

Run `uv sync` after creating `pyproject.toml` to generate the lockfile and venv.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .env.example .gitignore app/__init__.py app/config.py tests/test_config.py uv.lock
git commit -m "feat: project scaffolding and Settings config"
```

---

### Task 2: Diff parsing and application

**Files:**
- Create: `app/services/__init__.py`
- Create: `app/services/diffs.py`
- Test: `tests/test_diffs.py`

**Interfaces:**
- Produces: `ParsedDiff` dataclass (`target_paths: list[str]`, `deletes: bool`, `lines_changed: int`); `parse_unified_diff(diff_text: str) -> ParsedDiff`; `apply_diff(diff_text: str, workspace_root: Path) -> None` (raises `RuntimeError` on failure).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diffs.py
import subprocess
from pathlib import Path

import pytest

from app.services.diffs import apply_diff, parse_unified_diff


def _make_diff(before: str, after: str, path: str = "app.py") -> str:
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))


def test_parse_unified_diff_basic():
    diff = _make_diff("x = 1\n", "x = 2\n")
    parsed = parse_unified_diff(diff)
    assert parsed.target_paths == ["app.py"]
    assert parsed.deletes is False
    assert parsed.lines_changed == 2  # one removed, one added


def test_parse_unified_diff_detects_delete():
    diff = "--- a/app.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    parsed = parse_unified_diff(diff)
    assert parsed.deletes is True


def test_apply_diff_applies_to_workspace(tmp_path: Path):
    repo = tmp_path
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    diff = _make_diff("x = 1\n", "x = 2\n")

    apply_diff(diff, repo)

    assert (repo / "app.py").read_text() == "x = 2\n"


def test_apply_diff_raises_on_bad_diff(tmp_path: Path):
    repo = tmp_path
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    with pytest.raises(RuntimeError):
        apply_diff("not a real diff", repo)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diffs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/__init__.py
```

```python
# app/services/diffs.py
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedDiff:
    target_paths: list[str]
    deletes: bool
    lines_changed: int


_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$", re.MULTILINE)
_DELETE_RE = re.compile(r"^\+\+\+ /dev/null$", re.MULTILINE)


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    target_paths = [p.strip() for p in _FILE_HEADER_RE.findall(diff_text) if p.strip() != "/dev/null"]
    deletes = bool(_DELETE_RE.search(diff_text))
    lines_changed = sum(
        1
        for line in diff_text.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    )
    return ParsedDiff(target_paths=target_paths, deletes=deletes, lines_changed=lines_changed)


def apply_diff(diff_text: str, workspace_root: Path) -> None:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nofix", "-"],
        cwd=workspace_root,
        input=diff_text,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git apply failed: {result.stderr}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diffs.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/__init__.py app/services/diffs.py tests/test_diffs.py
git commit -m "feat: unified diff parsing and application"
```

---

### Task 3: Guardrails

**Files:**
- Create: `app/agent/__init__.py`
- Create: `app/agent/guardrails.py`
- Test: `tests/test_guardrails.py`

**Interfaces:**
- Consumes: `app.services.diffs.parse_unified_diff` (Task 2).
- Produces: `validate_diff(diff_text: str, target_path: str, forbidden_paths: list[str], max_lines: int) -> tuple[bool, str | None]`; `is_forbidden(path: str, forbidden_patterns: list[str]) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_guardrails.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/agent/__init__.py
```

```python
# app/agent/guardrails.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_guardrails.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agent/__init__.py app/agent/guardrails.py tests/test_guardrails.py
git commit -m "feat: guardrail diff validation"
```

---

### Task 4: Risk scoring

**Files:**
- Create: `app/agent/risk.py`
- Test: `tests/test_risk.py`

**Interfaces:**
- Consumes: `app.services.diffs.parse_unified_diff` (Task 2).
- Produces: `score(diff_text: str, has_test_coverage: bool) -> float` (0.0–1.0).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk.py
from app.agent.risk import score


def _diff_with(body: str, target: str = "app.py") -> str:
    return f"--- a/{target}\n+++ b/{target}\n@@ -1,1 +1,1 @@\n{body}"


def test_score_low_for_small_clean_diff():
    diff = _diff_with("+x = 2\n-x = 1\n")
    assert score(diff, has_test_coverage=True) < 0.3


def test_score_higher_without_test_coverage():
    diff = _diff_with("+x = 2\n-x = 1\n")
    with_tests = score(diff, has_test_coverage=True)
    without_tests = score(diff, has_test_coverage=False)
    assert without_tests > with_tests


def test_score_higher_for_sensitive_tokens():
    diff = _diff_with("+password = get_secret()\n-password = None\n")
    plain = _diff_with("+x = 2\n-x = 1\n")
    assert score(diff, has_test_coverage=True) > score(plain, has_test_coverage=True)


def test_score_capped_at_one():
    body = "".join(f"+password token secret session sql auth line{i}\n" for i in range(500))
    diff = _diff_with(body)
    assert score(diff, has_test_coverage=False) == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_risk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.risk'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/agent/risk.py
from app.services.diffs import parse_unified_diff

SENSITIVE_TOKENS = ["password", "secret", "token", "session", "sql", "auth"]
_LINES_NORMALIZER = 200


def score(diff_text: str, has_test_coverage: bool) -> float:
    parsed = parse_unified_diff(diff_text)
    lines_component = min(parsed.lines_changed / _LINES_NORMALIZER, 1.0) * 0.4
    lowered = diff_text.lower()
    sensitive_component = 0.4 if any(tok in lowered for tok in SENSITIVE_TOKENS) else 0.0
    test_component = 0.0 if has_test_coverage else 0.2
    return round(min(lines_component + sensitive_component + test_component, 1.0), 3)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_risk.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agent/risk.py tests/test_risk.py
git commit -m "feat: heuristic risk scoring"
```

---

### Task 5: Budget tracking

**Files:**
- Create: `app/agent/budget.py`
- Test: `tests/test_budget.py`

**Interfaces:**
- Produces: `PRICING: dict[str, dict[str, float]]`; `BudgetTracker` dataclass with `cap_usd: float`, `cost_used_usd: float = 0.0`, methods `cost_of(tokens_in: int, tokens_out: int, provider_name: str) -> float`, `would_exceed(estimated_cost: float) -> bool`, `record(cost: float) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_budget.py
import pytest

from app.agent.budget import BudgetTracker


def test_cost_of_uses_provider_pricing():
    tracker = BudgetTracker(cap_usd=2.00)
    cost = tracker.cost_of(tokens_in=1000, tokens_out=1000, provider_name="bedrock-primary")
    assert cost > 0


def test_cost_of_unknown_provider_raises():
    tracker = BudgetTracker(cap_usd=2.00)
    with pytest.raises(KeyError):
        tracker.cost_of(1000, 1000, "unknown-provider")


def test_would_exceed_false_when_under_cap():
    tracker = BudgetTracker(cap_usd=2.00, cost_used_usd=1.00)
    assert tracker.would_exceed(0.50) is False


def test_would_exceed_true_when_over_cap():
    tracker = BudgetTracker(cap_usd=2.00, cost_used_usd=1.80)
    assert tracker.would_exceed(0.50) is True


def test_record_accumulates_cost():
    tracker = BudgetTracker(cap_usd=2.00)
    tracker.record(0.30)
    tracker.record(0.20)
    assert tracker.cost_used_usd == pytest.approx(0.50)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.budget'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/agent/budget.py
from dataclasses import dataclass

PRICING = {
    "bedrock-primary": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "bedrock-fallback": {"input_per_1k": 0.0008, "output_per_1k": 0.004},
}


@dataclass
class BudgetTracker:
    cap_usd: float
    cost_used_usd: float = 0.0

    def cost_of(self, tokens_in: int, tokens_out: int, provider_name: str) -> float:
        pricing = PRICING[provider_name]
        return (tokens_in / 1000) * pricing["input_per_1k"] + (tokens_out / 1000) * pricing["output_per_1k"]

    def would_exceed(self, estimated_cost: float) -> bool:
        return (self.cost_used_usd + estimated_cost) > self.cap_usd

    def record(self, cost: float) -> None:
        self.cost_used_usd += cost
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_budget.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agent/budget.py tests/test_budget.py
git commit -m "feat: per-task budget tracking"
```

---

### Task 6: Provider router (Bedrock primary + fallback, retry/backoff)

**Files:**
- Create: `app/agent/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `ProviderError(Exception)`; `BedrockProvider(model_id: str, name: str, client)` with `.invoke(prompt: str) -> tuple[str, int, int]` (text, tokens_in, tokens_out); `ProviderRouter(primary: BedrockProvider, fallback: BedrockProvider, max_attempts: int = 3, backoff_base: float = 1.0)` with `.generate(prompt: str) -> tuple[str, int, int, str]` (text, tokens_in, tokens_out, provider_name).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_providers.py
import json
from unittest.mock import MagicMock

import pytest

from app.agent.providers import BedrockProvider, ProviderError, ProviderRouter


def _bedrock_response(text: str, tokens_in: int = 10, tokens_out: int = 20) -> MagicMock:
    body = MagicMock()
    body.read.return_value = json.dumps({
        "content": [{"text": text}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }).encode()
    return {"body": body}


def test_bedrock_provider_invoke_parses_response():
    client = MagicMock()
    client.invoke_model.return_value = _bedrock_response("hello world")
    provider = BedrockProvider("model-id", "bedrock-primary", client)

    text, tokens_in, tokens_out = provider.invoke("prompt")

    assert text == "hello world"
    assert tokens_in == 10
    assert tokens_out == 20


def test_router_uses_primary_on_success():
    primary_client = MagicMock()
    primary_client.invoke_model.return_value = _bedrock_response("primary result")
    fallback_client = MagicMock()

    router = ProviderRouter(
        BedrockProvider("primary-id", "bedrock-primary", primary_client),
        BedrockProvider("fallback-id", "bedrock-fallback", fallback_client),
        max_attempts=2,
        backoff_base=0,
    )

    text, tokens_in, tokens_out, provider_name = router.generate("prompt")

    assert text == "primary result"
    assert provider_name == "bedrock-primary"
    fallback_client.invoke_model.assert_not_called()


def test_router_falls_back_after_primary_exhausts_retries():
    primary_client = MagicMock()
    primary_client.invoke_model.side_effect = RuntimeError("throttled")
    fallback_client = MagicMock()
    fallback_client.invoke_model.return_value = _bedrock_response("fallback result")

    router = ProviderRouter(
        BedrockProvider("primary-id", "bedrock-primary", primary_client),
        BedrockProvider("fallback-id", "bedrock-fallback", fallback_client),
        max_attempts=2,
        backoff_base=0,
    )

    text, tokens_in, tokens_out, provider_name = router.generate("prompt")

    assert text == "fallback result"
    assert provider_name == "bedrock-fallback"
    assert primary_client.invoke_model.call_count == 2


def test_router_raises_provider_error_when_both_fail():
    primary_client = MagicMock()
    primary_client.invoke_model.side_effect = RuntimeError("primary down")
    fallback_client = MagicMock()
    fallback_client.invoke_model.side_effect = RuntimeError("fallback down")

    router = ProviderRouter(
        BedrockProvider("primary-id", "bedrock-primary", primary_client),
        BedrockProvider("fallback-id", "bedrock-fallback", fallback_client),
        max_attempts=2,
        backoff_base=0,
    )

    with pytest.raises(ProviderError):
        router.generate("prompt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.providers'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/agent/providers.py
import json
import logging
import time

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


class BedrockProvider:
    def __init__(self, model_id: str, name: str, client):
        self.model_id = model_id
        self.name = name
        self.client = client

    def invoke(self, prompt: str) -> tuple[str, int, int]:
        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        payload = json.loads(response["body"].read())
        text = payload["content"][0]["text"]
        usage = payload.get("usage", {})
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


class ProviderRouter:
    def __init__(
        self,
        primary: BedrockProvider,
        fallback: BedrockProvider,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
    ):
        self.primary = primary
        self.fallback = fallback
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base

    def generate(self, prompt: str) -> tuple[str, int, int, str]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                text, tokens_in, tokens_out = self.primary.invoke(prompt)
                return text, tokens_in, tokens_out, self.primary.name
            except Exception as exc:  # noqa: BLE001 - deliberately broad, any provider failure triggers failover
                last_exc = exc
                logger.warning("primary provider attempt %d failed: %s", attempt, exc)
                if attempt < self.max_attempts and self.backoff_base:
                    time.sleep(self.backoff_base * (2 ** (attempt - 1)))
        try:
            text, tokens_in, tokens_out = self.fallback.invoke(prompt)
            logger.warning("falling back to %s after primary exhausted retries", self.fallback.name)
            return text, tokens_in, tokens_out, self.fallback.name
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"both providers failed: primary={last_exc}, fallback={exc}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agent/providers.py tests/test_providers.py
git commit -m "feat: Bedrock provider router with retry and failover"
```

---

### Task 7: Test runner service

**Files:**
- Create: `app/services/tests_runner.py`
- Test: `tests/test_tests_runner.py`

**Interfaces:**
- Produces: `TestRunResult` dataclass (`passed: bool`, `output: str`); `run_tests(workspace_root: Path, test_command: str, timeout: int = 60) -> TestRunResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tests_runner.py
from pathlib import Path

from app.services.tests_runner import run_tests


def test_run_tests_passes_on_success(tmp_path: Path):
    result = run_tests(tmp_path, "true")
    assert result.passed is True


def test_run_tests_fails_on_nonzero_exit(tmp_path: Path):
    result = run_tests(tmp_path, "false")
    assert result.passed is False


def test_run_tests_captures_output(tmp_path: Path):
    result = run_tests(tmp_path, "echo hello-from-test")
    assert "hello-from-test" in result.output


def test_run_tests_handles_timeout(tmp_path: Path):
    result = run_tests(tmp_path, "sleep 5", timeout=1)
    assert result.passed is False
    assert "timed out" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tests_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tests_runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/tests_runner.py
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestRunResult:
    passed: bool
    output: str


def run_tests(workspace_root: Path, test_command: str, timeout: int = 60) -> TestRunResult:
    try:
        proc = subprocess.run(
            shlex.split(test_command),
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return TestRunResult(passed=False, output=f"test command timed out after {timeout}s: {exc}")
    return TestRunResult(passed=proc.returncode == 0, output=proc.stdout + proc.stderr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tests_runner.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/tests_runner.py tests/test_tests_runner.py
git commit -m "feat: subprocess test runner"
```

---

### Task 8: Graph core — state, nodes, graph wiring

**Files:**
- Create: `app/agent/state.py`
- Create: `app/agent/nodes.py`
- Create: `app/agent/graph.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: `app.agent.guardrails.validate_diff` (Task 3), `app.agent.risk.score` (Task 4), `app.agent.budget.BudgetTracker` (Task 5), `app.agent.providers.ProviderError` (Task 6), `app.services.diffs.apply_diff` (Task 2), `app.services.tests_runner.run_tests` (Task 7).
- Produces: `GraphState`, `FileResult`, `PlanEntry` TypedDicts; `NodeDeps` dataclass; `build_graph(deps: NodeDeps)` returning a compiled LangGraph graph with `thread_id`-scoped `MemorySaver` checkpointing. Any object passed as `deps.providers` need only implement `.generate(prompt: str) -> tuple[str, int, int, str]` (duck-typed, so a fake router works in tests and `ProviderRouter` works in production).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph.py
import difflib
import json
from pathlib import Path

from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps


class FakeProviderRouter:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt: str):
        text = self.responses[self.calls]
        self.calls += 1
        return text, 100, 50, "fake-provider"


def _make_diff(before: str, after: str, path: str = "app.py") -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))


def _initial_state(repo_path: str, goal: str, test_command: str) -> dict:
    return {
        "task_id": "test-task",
        "repo_path": repo_path,
        "goal": goal,
        "test_command": test_command,
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [],
    }


def test_migrate_graph_happy_path_low_risk(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "trivial change", "risk_score": 0.1}]),
        _make_diff("x = 1\n", "x = 2\n"),
    ])
    deps = NodeDeps(
        providers=fake,
        budget=BudgetTracker(cap_usd=10.0),
        forbidden_paths=[],
        max_diff_lines=400,
        risk_threshold=0.6,
        max_retries=2,
        estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "happy-path"}}

    result = graph.invoke(_initial_state(str(repo), "bump x", "true"), config=config)

    assert "__interrupt__" not in result
    assert result["files"]["app.py"]["status"] == "migrated"
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_migrate_graph_high_risk_requires_approval(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "risky change", "risk_score": 0.9}]),
        _make_diff("x = 1\n", "x = 2\n"),
    ])
    deps = NodeDeps(
        providers=fake,
        budget=BudgetTracker(cap_usd=10.0),
        forbidden_paths=[],
        max_diff_lines=400,
        risk_threshold=0.6,
        max_retries=2,
        estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "high-risk"}}

    result = graph.invoke(_initial_state(str(repo), "bump x", "true"), config=config)
    assert "__interrupt__" in result

    result = graph.invoke(Command(resume={"decision": "approve", "note": ""}), config=config)

    assert "__interrupt__" not in result
    assert result["files"]["app.py"]["status"] == "approved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.state'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/agent/state.py
from typing import Literal, Optional, TypedDict


class FileResult(TypedDict):
    path: str
    status: Literal["pending", "migrated", "approved", "rejected", "failed", "skipped"]
    tokens: int
    cost_usd: float
    retry_count: int
    last_error: Optional[str]


class PlanEntry(TypedDict):
    path: str
    rationale: str
    risk_score: float


class GraphState(TypedDict):
    task_id: str
    repo_path: str
    goal: str
    test_command: str
    plan: list[PlanEntry]
    files: dict[str, FileResult]
    cursor: int
    cost_used_usd: float
    trace: list[dict]
```

```python
# app/agent/nodes.py
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langgraph.types import interrupt

from app.agent.guardrails import validate_diff
from app.agent.providers import ProviderError
from app.agent.state import GraphState
from app.services.diffs import apply_diff
from app.services.tests_runner import run_tests


@dataclass
class NodeDeps:
    providers: object  # duck-typed: .generate(prompt) -> (text, tokens_in, tokens_out, provider_name)
    budget: object      # duck-typed: BudgetTracker interface
    forbidden_paths: list[str]
    max_diff_lines: int
    risk_threshold: float
    max_retries: int
    estimated_cost_per_file: float


def _is_test_file(path: Path) -> bool:
    return "tests" in path.parts or path.stem.startswith("test_") or path.stem.endswith("_test")


def ingest_node(state: GraphState, deps: NodeDeps) -> dict:
    workspace = Path(state["repo_path"])
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=agent@repomodernizer.local", "-c", "user.name=repomodernizer",
            "commit", "-q", "-m", "baseline",
        ],
        cwd=workspace, check=True,
    )
    return {
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [{"node": "ingest", "note": "workspace initialized"}],
    }


def plan_node(state: GraphState, deps: NodeDeps) -> dict:
    file_list = sorted(
        str(p.relative_to(state["repo_path"]))
        for p in Path(state["repo_path"]).rglob("*.py")
        if ".git" not in p.parts and not _is_test_file(p)
    )
    prompt = (
        f"You are planning a code migration. Goal: {state['goal']}\n"
        f"Files in repo:\n" + "\n".join(file_list) + "\n\n"
        "Return ONLY a JSON array of objects: "
        '[{"path": "...", "rationale": "...", "risk_score": 0.0}]. '
        "risk_score is 0-1. Order files by migration dependency order."
    )
    text, tokens_in, tokens_out, provider_name = deps.providers.generate(prompt)
    cost = deps.budget.cost_of(tokens_in, tokens_out, provider_name)
    deps.budget.record(cost)
    plan = json.loads(text)
    files = {
        entry["path"]: {
            "path": entry["path"],
            "status": "pending",
            "tokens": 0,
            "cost_usd": 0.0,
            "retry_count": 0,
            "last_error": None,
        }
        for entry in plan
    }
    return {
        "plan": plan,
        "files": files,
        "cost_used_usd": deps.budget.cost_used_usd,
        "trace": state["trace"] + [{"node": "plan", "note": f"{len(plan)} files planned"}],
    }


def _advance(state: GraphState, path: str, file_result: dict, deps: NodeDeps, note: str = "") -> dict:
    files = dict(state["files"])
    files[path] = file_result
    return {
        "files": files,
        "cursor": state["cursor"] + 1,
        "cost_used_usd": deps.budget.cost_used_usd,
        "trace": state["trace"] + [
            {"node": "migrate_file", "path": path, "status": file_result["status"], "note": note}
        ],
    }


def _retry_or_fail(state: GraphState, file_result: dict, path: str, error_msg: str, deps: NodeDeps) -> dict:
    file_result["retry_count"] += 1
    file_result["last_error"] = error_msg
    if file_result["retry_count"] > deps.max_retries:
        file_result["status"] = "failed"
        return _advance(state, path, file_result, deps, note=error_msg)
    files = dict(state["files"])
    files[path] = file_result
    return {
        "files": files,
        "cost_used_usd": deps.budget.cost_used_usd,
        "trace": state["trace"] + [{"node": "migrate_file", "path": path, "status": "retrying", "note": error_msg}],
    }


def migrate_file_node(state: GraphState, deps: NodeDeps) -> dict:
    entry = state["plan"][state["cursor"]]
    path = entry["path"]
    file_result = dict(state["files"][path])
    workspace = Path(state["repo_path"])

    if deps.budget.would_exceed(deps.estimated_cost_per_file):
        file_result["status"] = "skipped"
        return _advance(state, path, file_result, deps, note="budget cap reached")

    source = (workspace / path).read_text()
    error_context = f"\nPrevious attempt failed: {file_result['last_error']}" if file_result["last_error"] else ""
    prompt = (
        f"Goal: {state['goal']}\nFile: {path}\n\n{source}\n{error_context}\n\n"
        "Return ONLY a unified diff (git diff format) that migrates this file toward the goal. "
        "Do not touch any other file. Do not delete the file."
    )

    try:
        diff_text, tokens_in, tokens_out, provider_name = deps.providers.generate(prompt)
    except ProviderError as exc:
        file_result["status"] = "failed"
        return _advance(state, path, file_result, deps, note=f"provider error: {exc}")

    cost = deps.budget.cost_of(tokens_in, tokens_out, provider_name)
    deps.budget.record(cost)
    file_result["tokens"] += tokens_in + tokens_out
    file_result["cost_usd"] += cost

    ok, reason = validate_diff(diff_text, path, deps.forbidden_paths, deps.max_diff_lines)
    if not ok:
        return _retry_or_fail(state, file_result, path, f"guardrail: {reason}", deps)

    risk = entry.get("risk_score", 0.0)
    if risk >= deps.risk_threshold:
        decision = interrupt({"path": path, "diff": diff_text, "risk_score": risk})
        if decision.get("decision") != "approve":
            file_result["status"] = "rejected"
            return _advance(state, path, file_result, deps, note=decision.get("note", "rejected by reviewer"))

    apply_diff(diff_text, workspace)
    result = run_tests(workspace, state["test_command"])
    if not result.passed:
        return _retry_or_fail(state, file_result, path, f"tests failed: {result.output[-500:]}", deps)

    file_result["status"] = "approved" if risk >= deps.risk_threshold else "migrated"
    return _advance(state, path, file_result, deps)


def finalize_node(state: GraphState, deps: NodeDeps) -> dict:
    run_dir = Path(state["repo_path"]).parent
    (run_dir / "trace.json").write_text(json.dumps(state["trace"], indent=2))
    lines = ["| File | Status | Tokens | Cost ($) |", "|---|---|---|---|"]
    for path, result in state["files"].items():
        lines.append(f"| {path} | {result['status']} | {result['tokens']} | {result['cost_usd']:.4f} |")
    lines.append(f"\nTotal cost: ${state['cost_used_usd']:.4f}")
    (run_dir / "summary.md").write_text("\n".join(lines))
    return {"trace": state["trace"] + [{"node": "finalize", "note": "run complete"}]}
```

```python
# app/agent/graph.py
from functools import partial

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agent.nodes import NodeDeps, finalize_node, ingest_node, migrate_file_node, plan_node
from app.agent.state import GraphState


def route_after_migrate(state: GraphState) -> str:
    if state["cursor"] >= len(state["plan"]):
        return "finalize"
    return "migrate_file"


def build_graph(deps: NodeDeps):
    graph = StateGraph(GraphState)
    graph.add_node("ingest", partial(ingest_node, deps=deps))
    graph.add_node("plan", partial(plan_node, deps=deps))
    graph.add_node("migrate_file", partial(migrate_file_node, deps=deps))
    graph.add_node("finalize", partial(finalize_node, deps=deps))

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "plan")
    graph.add_conditional_edges("plan", route_after_migrate, {
        "migrate_file": "migrate_file",
        "finalize": "finalize",
    })
    graph.add_conditional_edges("migrate_file", route_after_migrate, {
        "migrate_file": "migrate_file",
        "finalize": "finalize",
    })
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=MemorySaver())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_graph.py -v`
Expected: PASS (2 tests). If LangGraph's interrupt/resume API differs from the signature used here (`result["__interrupt__"]`, `Command(resume=...)`), check the installed `langgraph` version's docs (`python -c "import langgraph; print(langgraph.__version__)"`) and adjust `graph.py`/`nodes.py` accordingly — the test will point at the exact mismatch.

- [ ] **Step 5: Commit**

```bash
git add app/agent/state.py app/agent/nodes.py app/agent/graph.py tests/test_graph.py
git commit -m "feat: LangGraph migration graph with interrupt-gated risk approval"
```

---

### Task 9: CLI entry point

**Files:**
- Create: `app/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `app.agent.graph.build_graph`, `app.agent.nodes.NodeDeps` (Task 8), `app.agent.providers.BedrockProvider`, `app.agent.providers.ProviderRouter` (Task 6), `app.agent.budget.BudgetTracker` (Task 5), `app.config.Settings` (Task 1).
- Produces: `main() -> None` (console entry point `repomod`), `run(repo_path: str, goal: str, test_command: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/cli.py
import argparse
import uuid
from pathlib import Path
from shutil import copytree

import boto3
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="repomod")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--repo", required=True)
    run_parser.add_argument("--goal", required=True)
    run_parser.add_argument("--test-cmd", required=True)
    args = parser.parse_args()

    if args.command == "run":
        run(args.repo, args.goal, args.test_cmd)


def run(repo_path: str, goal: str, test_command: str) -> None:
    settings = Settings()
    task_id = uuid.uuid4().hex[:8]
    run_dir = Path("runs") / task_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    copytree(repo_path, workspace, dirs_exist_ok=True)

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    providers = ProviderRouter(
        BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
        BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
    )
    deps = NodeDeps(
        providers=providers,
        budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=settings.forbidden_paths_list(),
        max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold,
        max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": task_id}}

    initial_state = {
        "task_id": task_id,
        "repo_path": str(workspace),
        "goal": goal,
        "test_command": test_command,
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [],
    }

    result = graph.invoke(initial_state, config=config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print(f"\nRisk gate triggered for {payload['path']} (risk={payload['risk_score']:.2f})")
        print(payload["diff"])
        decision = input("approve/reject: ").strip().lower()
        note = "" if decision == "approve" else input("reason: ").strip()
        result = graph.invoke(Command(resume={"decision": decision, "note": note}), config=config)

    print(f"\nDone. task_id={task_id}, cost=${result['cost_used_usd']:.4f}")
    print(f"Trace + summary written to {run_dir}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/cli.py tests/test_cli.py
git commit -m "feat: repomod CLI entry point"
```

---

### Task 10: Sample fixture repo + live integration test + README

**Files:**
- Create: `fixtures/sample_repo/webapp.py`
- Create: `fixtures/sample_repo/conftest.py`
- Create: `fixtures/sample_repo/tests/test_app.py`
- Create: `fixtures/sample_repo/requirements.txt`
- Create: `tests/test_graph_integration.py`
- Create: `README.md`

**Interfaces:**
- Consumes: everything from Tasks 1–8 (real `BedrockProvider`/`ProviderRouter`, `build_graph`, `Settings`) — this task exercises the whole system against real Bedrock, no new production interfaces produced.

Note: the fixture's app module is named `webapp.py`, not `app.py` — the main project already has a top-level `app/` package, and naming the fixture module `app.py` would create an import collision (`from app import app` could resolve to either one depending on sys.path order). `webapp.py` sidesteps that ambiguity entirely.

- [ ] **Step 1: Write the fixture repo**

```python
# fixtures/sample_repo/webapp.py
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/items/<int:item_id>")
def get_item(item_id: int):
    return jsonify(id=item_id, name=f"item-{item_id}")


if __name__ == "__main__":
    app.run()
```

```python
# fixtures/sample_repo/conftest.py
# Empty on purpose: its presence makes pytest insert this directory into
# sys.path, so `from webapp import app` resolves inside tests/test_app.py.
```

```python
# fixtures/sample_repo/tests/test_app.py
# Framework-agnostic on purpose: passes against both the original Flask webapp.py
# and a migrated FastAPI webapp.py, so this file itself is excluded from the
# migration plan (see nodes.py:_is_test_file) and stays fixed as the source of truth.
from webapp import app as application


def _client():
    if hasattr(application, "test_client"):
        return application.test_client()
    from fastapi.testclient import TestClient
    return TestClient(application)


def _json(response):
    if hasattr(response, "get_json"):
        return response.get_json()
    return response.json()


def test_health():
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert _json(response) == {"status": "ok"}


def test_get_item():
    client = _client()
    response = client.get("/items/42")
    assert response.status_code == 200
    assert _json(response) == {"id": 42, "name": "item-42"}
```

```
# fixtures/sample_repo/requirements.txt
flask
fastapi
httpx
pytest
```

- [ ] **Step 2: Confirm the fixture passes on its own, unmigrated**

Run: `uv run pytest fixtures/sample_repo -q`
Expected: PASS (2 tests) — proves the fixture and its framework-agnostic test client work before any migration runs. Uses the `flask`/`fastapi`/`httpx`/`pytest` dev-dependencies already installed via `uv sync` in Task 1.

- [ ] **Step 3: Write the live integration test**

```python
# tests/test_graph_integration.py
import os
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BEDROCK_TESTS") != "1",
    reason="set RUN_LIVE_BEDROCK_TESTS=1 to run this test (calls real Bedrock, costs money)",
)


def test_migrate_sample_repo_flask_to_fastapi(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(Path(__file__).parent.parent / "fixtures" / "sample_repo", workspace)

    settings = Settings()
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    providers = ProviderRouter(
        BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
        BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
    )
    deps = NodeDeps(
        providers=providers,
        budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=settings.forbidden_paths_list(),
        max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold,
        max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "integration-test"}}
    initial_state = {
        "task_id": "integration-test",
        "repo_path": str(workspace),
        "goal": "Migrate this Flask app to FastAPI with async route handlers, preserving behavior.",
        "test_command": "pytest -q",
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [],
    }

    result = graph.invoke(initial_state, config=config)
    while "__interrupt__" in result:
        result = graph.invoke(Command(resume={"decision": "approve", "note": "auto-approved in test"}), config=config)

    assert result["files"]["webapp.py"]["status"] in ("migrated", "approved")

    final = subprocess.run(["pytest", "-q"], cwd=workspace, capture_output=True, text=True)
    assert final.returncode == 0, final.stdout + final.stderr
```

- [ ] **Step 4: Run the live integration test**

Run: `RUN_LIVE_BEDROCK_TESTS=1 uv run pytest tests/test_graph_integration.py -v -s`
Expected: PASS — `app.py` migrated to FastAPI, and the fixture's framework-agnostic test suite passes against the migrated app. Requires valid AWS credentials with Bedrock access in the environment. Record this run (terminal output showing the risk-gate prompt and final summary) — it's the interview demo.

- [ ] **Step 5: Write the README**

```markdown
# RepoModernizer — Local Agent Core

Sub-project 1 of RepoModernizer: a LangGraph-driven agent that migrates a repo file-by-file
toward a stated goal, gated by guardrails, risk scoring, and a cost cap, with human-in-the-loop
approval on risky diffs. Runs entirely locally — no AWS infra beyond Bedrock inference calls.

Full project spec: [`RepoModernizer-Spec.md`](../RepoModernizer-Spec.md).
This sub-project's design: [`docs/superpowers/specs/2026-07-27-local-agent-core-design.md`](docs/superpowers/specs/2026-07-27-local-agent-core-design.md).

## Setup

```bash
uv sync
cp .env.example .env   # fill in AWS credentials with Bedrock access
```

## Run a migration

```bash
uv run repomod run --repo ./fixtures/sample_repo --goal "Flask to FastAPI async" --test-cmd "pytest -q"
```

Output (trace, summary, per-file diffs) is written to `runs/<task_id>/`.

## Tests

```bash
uv run pytest -q                              # fast unit tests, no network
RUN_LIVE_BEDROCK_TESTS=1 uv run pytest -q -s  # + live end-to-end migration against fixtures/sample_repo
```
```

- [ ] **Step 6: Commit**

```bash
git add fixtures/sample_repo tests/test_graph_integration.py README.md
git commit -m "feat: sample fixture repo, live integration test, README"
```
