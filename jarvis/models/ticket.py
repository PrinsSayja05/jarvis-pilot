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


@dataclass
class TicketComment:
    author: str
    created: str
    body: str
