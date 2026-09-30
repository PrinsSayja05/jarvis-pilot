from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str
    issue_type: str
    status: str
    labels: list[str] = field(default_factory=list)
    url: str = ""
    acceptance_criteria: str = ""
    comments: list[TicketComment] = field(default_factory=list)
    assignee_id: str = ""       # Jira accountId
    assignee_name: str = ""
    priority: str = ""          # Jira name: Highest, High, Medium, Low, Lowest (also "Low (migrated)")


# Ranking by meaning, not by Jira's list order (there "Low" comes after "Lowest").
PRIORITY_RANK = {"highest": 0, "high": 1, "medium": 2, "low": 3, "low (migrated)": 3, "lowest": 4}
PRIORITY_DE = {"highest": "Höchste", "high": "Hoch", "medium": "Mittel", "low": "Niedrig", "low (migrated)": "Niedrig", "lowest": "Niedrigste"}


def priority_rank(name: str) -> int:
    return PRIORITY_RANK.get((name or "").strip().lower(), 2)


def is_urgent(name: str) -> bool:
    return priority_rank(name) <= 1


def priority_label(name: str) -> str:
    """'Hoch (High)' for PR bodies and logs; empty when Jira has no priority."""
    return f"{PRIORITY_DE.get(name.strip().lower(), name)} ({name.replace(' (migrated)', '')})" if name else ""


@dataclass
class TicketComment:
    author: str
    created: str
    body: str
