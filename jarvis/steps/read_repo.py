"""Clone the target repo and build a lightweight repo map for planning."""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.clients.github_client import GitHubClient
from jarvis.config import JarvisConfig

logger = logging.getLogger("jarvis.read_repo")

_IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
_MAX_FILES = 500


@dataclass
class RepoMap:
    repo_full_name: str
    local_path: Path
    files: list[str] = field(default_factory=list)


def read_repo(repo_full_name: str, config: JarvisConfig) -> RepoMap:
    """Clone into a fresh temp dir. The dir lives until the run ends (code_change and run_tests read
    from it); the pipeline removes it with remove_clone(). If this function fails, it removes it itself."""
    workdir = Path(tempfile.mkdtemp(prefix=CLONE_PREFIX))
    try:
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
    except BaseException:
        remove_clone(workdir)
        raise
    return RepoMap(repo_full_name=repo_full_name, local_path=workdir, files=files)


CLONE_PREFIX = "jarvis-repo-"
STALE_CLONE_SECONDS = 60 * 60


def remove_clone(path: Path) -> None:
    """Delete a clone dir. git marks object files read-only, which a plain rmtree cannot delete on Windows."""
    def make_writable_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    path = Path(path).resolve()
    # Only ever our own clone dirs directly in the temp dir: a wrong path must never delete anything else.
    if path.parent != Path(tempfile.gettempdir()).resolve() or not path.name.startswith(CLONE_PREFIX):
        logger.warning("refusing to remove %s: not a JARVIS clone dir", path)
        return
    if not path.exists():
        return
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError as exc:  # cleanup must never fail a run
        logger.warning("could not remove clone %s: %s", path, exc)


def cleanup_stale_clones(max_age_seconds: int = STALE_CLONE_SECONDS) -> int:
    """Remove jarvis-repo-* dirs older than max_age_seconds, left over from crashed or old runs.
    Called once when the CLI or the console starts. Returns how many were removed."""
    removed = 0
    now = time.time()
    for path in Path(tempfile.gettempdir()).glob(CLONE_PREFIX + "*"):
        try:
            if path.is_dir() and now - path.stat().st_mtime > max_age_seconds:
                remove_clone(path)
                removed += not path.exists()
        except OSError:
            continue
    if removed:
        logger.info("removed %d stale clone dir(s) older than %d min", removed, max_age_seconds // 60)
    return removed


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
