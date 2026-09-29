from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReviewResult:
    passed: bool
    findings: str
    latency_seconds: float = 0.0
