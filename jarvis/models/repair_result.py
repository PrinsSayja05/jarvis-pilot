from __future__ import annotations

from dataclasses import dataclass

from jarvis.models.code_change import CodeChange
from jarvis.models.test_result import TestResult


@dataclass
class RepairResult:
    attempts: int
    final_test_result: TestResult
    success: bool
    final_change: CodeChange
