import subprocess
from pathlib import Path

from app.services.github import clone_repo, commit_all, create_branch


def _init_bare_remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "webapp.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "-q", "-m", "init"],
        cwd=seed, check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
    return bare


def test_clone_repo_clones_local_bare_remote(tmp_path: Path):
    bare = _init_bare_remote(tmp_path)
    dest = tmp_path / "clone"

    clone_repo(str(bare), dest, token="")

    assert (dest / "webapp.py").exists()


def test_create_branch_and_commit_all(tmp_path: Path):
    bare = _init_bare_remote(tmp_path)
    dest = tmp_path / "clone"
    clone_repo(str(bare), dest, token="")

    create_branch(dest, "feature/test")
    (dest / "webapp.py").write_text("x = 2\n")
    commit_all(dest, "bump x")

    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=dest, check=True, capture_output=True, text=True)
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=dest, check=True, capture_output=True, text=True)
    assert log.stdout.strip() == "bump x"
    assert branch.stdout.strip() == "feature/test"
