from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from jarvis.models.code_change import CodeChange
from jarvis.models.plan import Plan
from jarvis.models.pr_result import PullRequestResult
from jarvis.models.repair_result import RepairResult
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.state import RunState


@dataclass
class RunResult:
    run_id: str
    ticket_id: str
    state: RunState
    plan: Optional[Plan] = None
    approved: bool = False
    approver: Optional[str] = None
    code_change: Optional[CodeChange] = None
    test_result: Optional[TestResult] = None
    repair_result: Optional[RepairResult] = None
    artifact_key: Optional[str] = None
    review_result: Optional[ReviewResult] = None
    pr_result: Optional[PullRequestResult] = None
    started_at: str = ""
    ended_at: str = ""
    error: Optional[str] = None
