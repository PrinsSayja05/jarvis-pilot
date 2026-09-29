from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TestResult:
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    passed_count: int = 0
    failed: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
