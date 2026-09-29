from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RiskClass = Literal["low", "medium", "high"]


@dataclass
class FileChange:
    path: str
    reason: str


@dataclass
class Plan:
    files_to_change: list[FileChange]
    approach: str
    test_plan: list[str]
    risk_class: RiskClass
    estimated_tokens: int
    latency_seconds: float = 0.0
