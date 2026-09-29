"""Local Docker sandbox - runs the test command in an isolated container.

The files are written to a throwaway temp dir that is mounted at /app; the
container has no network. Build the image once: docker build -t jarvis-sandbox:latest ./sandbox/
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import SandboxConfig


class SandboxError(RuntimeError):
    """Raised when the sandbox itself cannot run (Docker missing, bad input, timeout)."""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    files: dict[str, str]


# docker run itself failed (daemon down, image missing) - not the tested command.
_DOCKER_ERROR_EXIT_CODE = 125


class SandboxClient:
    def __init__(self, settings: SandboxConfig) -> None:
        self._settings = settings

    def run(
        self,
        *,
        image: str,
        command: list[str],
        files: dict[str, str],
        timeout: int,
        network: str,
        check: bool = False,
    ) -> SandboxResult:
        """Run `command` in a container with `files` mounted at /app.

        A non-zero exit_code raises only with check=True - failing tests are a
        normal outcome that run_tests/repair must see.
        """
        # Rule: sandbox runs with network=none - enforced here, not just in config.
        if network != "none":
            raise SandboxError(f"Sandbox must run with network=none, got {network!r}")

        with tempfile.TemporaryDirectory(prefix="jarvis-sandbox-") as tmp:
            workdir = Path(tmp).resolve()
            _write_files(workdir, files)

            name = f"jarvis-sandbox-{uuid.uuid4().hex[:12]}"
            docker_cmd = [
                "docker", "run", "--rm", "--name", name,
                "--network", "none",
                *_host_user_args(),
                "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
                "-e", "PYTHONPATH=/app", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", f"{workdir}:/app",
                image, *command,
            ]
            try:
                proc = subprocess.run(
                    docker_cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                subprocess.run(["docker", "kill", name], capture_output=True)
                raise SandboxError(f"Sandbox command timed out after {timeout}s") from exc
            except FileNotFoundError as exc:
                raise SandboxError("Docker is not installed or not on PATH") from exc

        if proc.returncode == _DOCKER_ERROR_EXIT_CODE:
            raise SandboxError(f"docker run failed: {proc.stderr.strip()[:500]}")

        result = SandboxResult(
            exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, files={}
        )
        if check and result.exit_code != 0:
            raise SandboxError(f"Sandbox command exited with {result.exit_code}: {result.stderr[:500]}")
        return result


def _host_user_args() -> list[str]:
    """Run the container as the host user, so what it writes into the mount (e.g. .pytest_cache) is ours.

    As root it leaves root-owned files behind, and TemporaryDirectory's cleanup then fails with
    "[Errno 1] Operation not permitted". Windows (Docker Desktop) has no uid and no ownership problem.
    """
    if not hasattr(os, "getuid"):
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = (root / relative_path).resolve()
        if root not in target.parents:
            raise SandboxError(f"Refusing to write outside the sandbox dir: {relative_path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # A UTF-8 BOM breaks TOML/JSON parsers (e.g. pytest reading pyproject.toml).
        target.write_text(content.lstrip("﻿"), encoding="utf-8", newline="")
