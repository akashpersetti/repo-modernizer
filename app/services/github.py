import subprocess
from pathlib import Path

import httpx


def _with_token(url: str, token: str) -> str:
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://x-access-token:{token}@")
    return url


def clone_repo(repo_url: str, dest: Path, token: str) -> None:
    subprocess.run(["git", "clone", _with_token(repo_url, token), str(dest)], check=True, capture_output=True, text=True)


def create_branch(repo_path: Path, branch_name: str) -> None:
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_path, check=True, capture_output=True, text=True)


def commit_all(repo_path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git", "-c", "user.email=agent@repomodernizer.local", "-c", "user.name=repomodernizer",
            "commit", "-q", "-m", message, "--allow-empty",
        ],
        cwd=repo_path, check=True, capture_output=True, text=True,
    )


def push_branch(repo_path: Path, branch_name: str, token: str) -> None:
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "remote", "set-url", "origin", _with_token(remote_url, token)],
        cwd=repo_path, check=True, capture_output=True, text=True,
    )
    subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=repo_path, check=True, capture_output=True, text=True)


def _parse_owner_repo(repo_url: str) -> tuple[str, str]:
    cleaned = repo_url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    parts = cleaned.split("/")
    return parts[-2], parts[-1]


def open_pull_request(repo_url: str, branch: str, base: str, title: str, body: str, token: str) -> str:
    owner, repo = _parse_owner_repo(repo_url)
    response = httpx.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "head": branch, "base": base, "body": body},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["html_url"]
