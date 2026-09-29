from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CodeChange:
    files: dict[str, str]
    diff: str
    files_changed: list[str] = field(default_factory=list)
    tokens_used: int = 0
    latency_seconds: float = 0.0


CodeChangeResult = CodeChange
