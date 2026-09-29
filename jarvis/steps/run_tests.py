"""Run the test suite in the isolated sandbox (network:none)."""
from __future__ import annotations

import re
import shlex
import time

from jarvis.clients.sandbox_client import SandboxClient
from jarvis.config import JarvisConfig
from jarvis.models.code_change import CodeChange
from jarvis.models.test_result import TestResult
from jarvis.steps.read_repo import RepoMap


def run_tests(code_change: CodeChange, repo_map: RepoMap, config: JarvisConfig) -> TestResult:
    files = _read_repo_files(repo_map)
    files.update(code_change.files)

    client = SandboxClient(config.sandbox)
    start = time.monotonic()
    result = client.run(
        image=config.sandbox.image,
        command=shlex.split(config.sandbox.test_command),
        files=files,
        timeout=config.sandbox.timeout_seconds,
        network=config.sandbox.network,
    )
    duration = time.monotonic() - start

    return TestResult(
        passed=result.exit_code == 0,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        passed_count=_count(result.stdout, "passed"),
        failed=_count(result.stdout, "failed"),
        errors=_count(result.stdout, "errors?"),
        duration_seconds=duration,
    )


def _count(output: str, label: str) -> int:
    # pytest -q summary line, e.g. "2 failed, 5 passed, 1 error in 0.42s"
    matches = re.findall(rf"(\d+) {label}\b", output)
    return int(matches[-1]) if matches else 0


def _read_repo_files(repo_map: RepoMap) -> dict[str, str]:
    files: dict[str, str] = {}
    for relative_path in repo_map.files:
        path = repo_map.local_path / relative_path
        try:
            files[relative_path] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files
