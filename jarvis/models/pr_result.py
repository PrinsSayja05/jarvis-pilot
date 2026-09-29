from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PullRequestResult:
    url: str
    branch: str
