from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "base")
    return repo


def run(repo: Path, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
