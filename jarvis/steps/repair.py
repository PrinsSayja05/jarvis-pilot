"""If tests fail, ask jarvis-coder-large to fix the code (bounded retries)."""
from __future__ import annotations

import re
from typing import Callable

from jarvis.config import JarvisConfig
from jarvis.models.code_change import CodeChange
from jarvis.models.plan import Plan
from jarvis.models.repair_result import RepairResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket
from jarvis.steps.code_change import code_change as generate_code_change
from jarvis.steps.diff_quality import DiffQualityError
from jarvis.steps.read_repo import RepoMap
from jarvis.steps.run_tests import run_tests

MAX_REPAIR_ATTEMPTS = 3
_MAX_FEEDBACK_CHARS = 6000


class SmartRepair:
    """Bounded repair loop that feeds the coder a focused failure report.

    - Sends only the failing part of the pytest output, not the whole log.
    - Includes the previous diff so the model does not repeat it.
    - Skips the sandbox run when the model returns the same diff again.
    - Tells the model when its last fix left the failures unchanged.
    """

    def __init__(
        self,
        config: JarvisConfig,
        max_attempts: int = MAX_REPAIR_ATTEMPTS,
        on_attempt: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._max_attempts = min(max_attempts, config.limits.max_repair_loops)
        self._on_attempt = on_attempt or (lambda message: None)

    def repair(
        self,
        ticket: JiraTicket,
        plan: Plan,
        repo_map: RepoMap,
        change: CodeChange,
        test_result: TestResult,
    ) -> RepairResult:
        attempts = 0
        signature = _failure_signature(test_result)
        stalled = False
        coder_latency = change.latency_seconds

        while attempts < self._max_attempts and not test_result.passed:
            attempts += 1
            self._on_attempt(f"repair attempt {attempts}/{self._max_attempts}")

            feedback = self._build_feedback(change, test_result, stalled=stalled)
            try:
                candidate = generate_code_change(ticket, plan, repo_map, self._config, feedback=feedback)
            except DiffQualityError as exc:
                self._on_attempt(f"repair attempt {attempts}: unusable diff ({exc})")
                continue

            coder_latency += candidate.latency_seconds
            if candidate.diff == change.diff:
                self._on_attempt(f"repair attempt {attempts}: model returned the same diff, skipping tests")
                continue

            change = candidate
            change.latency_seconds = coder_latency  # cumulative across repair attempts
            test_result = run_tests(change, repo_map, self._config)
            new_signature = _failure_signature(test_result)
            stalled = new_signature == signature
            signature = new_signature
            self._on_attempt(
                f"repair attempt {attempts}: passed={test_result.passed} "
                f"failed={test_result.failed} errors={test_result.errors}"
            )

        return RepairResult(
            attempts=attempts,
            final_test_result=test_result,
            success=test_result.passed,
            final_change=change,
        )

    def _build_feedback(self, change: CodeChange, test_result: TestResult, *, stalled: bool) -> str:
        parts = [
            f"Tests failed (exit_code={test_result.exit_code}, failed={test_result.failed}, "
            f"errors={test_result.errors}).",
            "Failure output:\n" + _focus_failures(test_result),
            "Your previous diff (do not repeat it; return a complete new diff against the ORIGINAL files):\n"
            + change.diff,
        ]
        if stalled:
            parts.append("Your last fix left the same tests failing. Try a different approach.")
        return "\n\n".join(parts)


def repair(
    ticket: JiraTicket,
    plan: Plan,
    repo_map: RepoMap,
    change: CodeChange,
    test_result: TestResult,
    config: JarvisConfig,
    on_attempt: Callable[[str], None] | None = None,
) -> RepairResult:
    return SmartRepair(config, on_attempt=on_attempt).repair(ticket, plan, repo_map, change, test_result)


def _focus_failures(test_result: TestResult) -> str:
    output = test_result.stdout
    match = re.search(r"={3,} (FAILURES|ERRORS) ={3,}", output)
    focused = output[match.start():] if match else output
    if test_result.stderr.strip():
        focused += "\n\nstderr:\n" + test_result.stderr
    if len(focused) > _MAX_FEEDBACK_CHARS:
        focused = focused[:_MAX_FEEDBACK_CHARS] + "\n... (truncated)"
    return focused


def _failure_signature(test_result: TestResult) -> frozenset[str]:
    return frozenset(re.findall(r"^(?:FAILED|ERROR) (\S+)", test_result.stdout, flags=re.MULTILINE))
