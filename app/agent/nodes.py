import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langgraph.types import interrupt

from app.agent.guardrails import validate_diff
from app.agent.providers import ProviderError
from app.agent.state import GraphState
from app.services.dependencies import detect_manifest, install_dependencies
from app.services.diffs import apply_diff, make_diff
from app.services.tests_runner import run_tests

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Models often wrap output in markdown fences despite "return ONLY ..." instructions.

    Only touches text that actually matches a fence — a diff without one is returned
    byte-for-byte, since git apply is sensitive to trailing newlines.
    """
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1) + "\n" if match else text


@dataclass
class NodeDeps:
    providers: object  # duck-typed: .generate(prompt) -> (text, tokens_in, tokens_out, provider_name)
    budget: object      # duck-typed: BudgetTracker interface
    forbidden_paths: list[str]
    max_diff_lines: int
    risk_threshold: float
    max_retries: int
    estimated_cost_per_file: float


_VENDOR_DIR_NAMES = {"node_modules", "venv", ".venv", "dist", "build", "vendor", "site-packages"}


def _is_test_file(path: Path) -> bool:
    parts = set(path.parts)
    if "tests" in parts or "__tests__" in parts:
        return True
    stem = path.stem  # Path.stem only strips the final suffix: "App.test.js" -> "App.test"
    return (
        stem == "conftest"
        or stem.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith(".test")
        or stem.endswith(".spec")
    )


def _is_vendor_dir(path: Path) -> bool:
    return bool(set(path.parts) & _VENDOR_DIR_NAMES)


def ingest_node(state: GraphState, deps: NodeDeps) -> dict:
    workspace = Path(state["repo_path"])
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=agent@repomodernizer.local", "-c", "user.name=repomodernizer",
            "commit", "-q", "-m", "baseline", "--allow-empty",
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


def install_deps_node(state: GraphState, deps: NodeDeps) -> dict:
    workspace = Path(state["repo_path"])
    manifest = detect_manifest(workspace)

    if manifest is None:
        return {
            "venv_bin": None,
            "dependency_install_failed": False,
            "trace": state["trace"] + [{"node": "install_deps", "note": "no dependency manifest found, skipping install"}],
        }

    result = install_dependencies(workspace, manifest)

    if not result.ok:
        return {
            "venv_bin": None,
            "dependency_install_failed": True,
            "trace": state["trace"] + [{"node": "install_deps", "note": f"install failed: {result.output}"}],
        }

    return {
        "venv_bin": str(result.venv_bin),
        "dependency_install_failed": False,
        "trace": state["trace"] + [{"node": "install_deps", "note": f"installed dependencies via {manifest.kind}"}],
    }


def plan_node(state: GraphState, deps: NodeDeps) -> dict:
    root = Path(state["repo_path"])
    file_list = sorted(set(
        str(p.relative_to(root))
        for ext in state["file_extensions"]
        for p in root.rglob(f"*{ext}")
        if ".git" not in p.parts and not _is_test_file(p) and not _is_vendor_dir(p)
    ))
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
    plan = json.loads(_strip_code_fence(text))
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

    # make_diff needs `before` to genuinely match the on-disk file, including its
    # real trailing-newline status -- git apply matches context against actual
    # bytes, and difflib never emits the "\ No newline at end of file" marker a
    # diff against a no-final-newline file would need. Normalizing here (a
    # harmless, standard change) means every file we ever diff against ends with
    # "\n", so that whole class of git-apply failure can't happen. Found live
    # against a real JS repo (Python fixtures never exercised this — Python
    # source conventionally ends with a newline; plenty of hand-written JS/JSON
    # doesn't).
    target_file = workspace / path
    raw = target_file.read_bytes()
    if raw and not raw.endswith(b"\n"):
        target_file.write_bytes(raw + b"\n")
    source = target_file.read_text()
    error_context = f"\nPrevious attempt failed: {file_result['last_error']}" if file_result["last_error"] else ""
    prompt = (
        f"Goal: {state['goal']}\nFile: {path}\n\n{source}\n{error_context}\n\n"
        "Return ONLY the complete new content of this file after migrating it toward the goal. "
        "Do not include any explanation, markdown fences, or diff syntax — just the raw file content, "
        "nothing else. Keep the file non-empty."
    )

    try:
        new_content, tokens_in, tokens_out, provider_name = deps.providers.generate(prompt)
        new_content = _strip_code_fence(new_content)
    except ProviderError as exc:
        file_result["status"] = "failed"
        return _advance(state, path, file_result, deps, note=f"provider error: {exc}")

    diff_text = make_diff(source, new_content, path)

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

    try:
        apply_diff(diff_text, workspace)
    except RuntimeError as exc:
        return _retry_or_fail(state, file_result, path, f"diff apply failed: {exc}", deps)

    result = run_tests(workspace, state["test_command"], state.get("venv_bin"))
    if not result.passed:
        return _retry_or_fail(state, file_result, path, f"tests failed: {result.output[-500:]}", deps)

    file_result["status"] = "approved" if risk >= deps.risk_threshold else "migrated"
    return _advance(state, path, file_result, deps)


def finalize_node(state: GraphState, deps: NodeDeps) -> dict:
    run_dir = Path(state["repo_path"]).parent
    (run_dir / "trace.json").write_text(json.dumps(state["trace"], indent=2))
    if state.get("dependency_install_failed"):
        (run_dir / "summary.md").write_text(
            "Dependency installation failed -- task stopped before any files were migrated.\n"
        )
        return {"trace": state["trace"] + [{"node": "finalize", "note": "run complete (dependency install failed)"}]}
    lines = ["| File | Status | Tokens | Cost ($) |", "|---|---|---|---|"]
    for path, result in state["files"].items():
        lines.append(f"| {path} | {result['status']} | {result['tokens']} | {result['cost_usd']:.4f} |")
    lines.append(f"\nTotal cost: ${state['cost_used_usd']:.4f}")
    (run_dir / "summary.md").write_text("\n".join(lines))
    return {"trace": state["trace"] + [{"node": "finalize", "note": "run complete"}]}
