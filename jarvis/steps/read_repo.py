"""Clone the target repo and build a lightweight repo map for planning."""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.clients.github_client import GitHubClient
from jarvis.config import JarvisConfig

_IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
_MAX_FILES = 500


@dataclass
class RepoMap:
    repo_full_name: str
    local_path: Path
    files: list[str] = field(default_factory=list)


def read_repo(repo_full_name: str, config: JarvisConfig) -> RepoMap:
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-repo-"))
    token = GitHubClient(config.github).token
    clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

    proc = subprocess.run(
        # autocrlf=false: keep the blob's real line endings, so the PR diff is not a whole-file rewrite.
        ["git", "-c", "core.autocrlf=false", "clone", "--depth", "1", clone_url, str(workdir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Never let CalledProcessError carry the tokenized clone URL into logs.
        stderr = proc.stderr.replace(token, "***").strip()
        raise RuntimeError(f"git clone of {repo_full_name} failed (exit {proc.returncode}): {stderr}")

    files = _build_file_list(workdir)
    return RepoMap(repo_full_name=repo_full_name, local_path=workdir, files=files)


def _build_file_list(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        files.append(path.relative_to(root).as_posix())
        if len(files) >= _MAX_FILES:
            break
    return files
