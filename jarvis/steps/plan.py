"""Call the planner model (jarvis-general) and return a Plan."""
from __future__ import annotations

import json

from jarvis.clients.litellm_client import LiteLLMClient
from jarvis.config import JarvisConfig
from jarvis.models.plan import FileChange, Plan
from jarvis.models.ticket import JiraTicket
from jarvis.steps.read_repo import RepoMap

_SYSTEM_PROMPT = """You are a senior software engineer planning a code change.
Given a Jira ticket (description, acceptance criteria, labels and discussion) and a
repository file listing, produce a JSON plan with:
- files_to_change: list of {"path": str, "reason": str}
- approach: str, what will change and why
- test_plan: list of test names/descriptions to run
- risk_class: one of "low", "medium", "high"

The plan must satisfy every acceptance criterion. Treat later comments as
clarifications that override the description where they conflict.

Respond with JSON only, no prose."""

_MAX_DESCRIPTION_CHARS = 6000
_MAX_COMMENTS = 10  # the most recent ones
_MAX_COMMENT_CHARS = 1000


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " … (truncated)"


def _ticket_context(ticket: JiraTicket) -> str:
    parts = [
        f"Ticket {ticket.key} ({ticket.issue_type or 'issue'}, status: {ticket.status or '?'}): {ticket.summary}",
        f"Labels: {', '.join(ticket.labels) if ticket.labels else 'none'}",
        f"Description:\n{_clip(ticket.description, _MAX_DESCRIPTION_CHARS) or '(none)'}",
        f"Acceptance criteria:\n{_clip(ticket.acceptance_criteria, _MAX_DESCRIPTION_CHARS) or '(none given)'}",
    ]
    if ticket.comments:
        recent = ticket.comments[-_MAX_COMMENTS:]
        parts.append(
            f"Comments ({len(recent)} most recent, oldest first):\n"
            + "\n".join(f"- {c.author} ({c.created}): {_clip(c.body, _MAX_COMMENT_CHARS)}" for c in recent)
        )
    return "\n\n".join(parts)


def create_plan(ticket: JiraTicket, repo_map: RepoMap, config: JarvisConfig) -> Plan:
    client = LiteLLMClient(config.litellm)

    user_prompt = (
        _ticket_context(ticket) + "\n\n"
        f"Repository: {repo_map.repo_full_name}\n"
        f"Files ({len(repo_map.files)}):\n" + "\n".join(repo_map.files[:200])
    )

    result = client.complete(
        model_alias=config.models.planner,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    data = json.loads(result.content)

    return Plan(
        files_to_change=[FileChange(**fc) for fc in data["files_to_change"]],
        approach=data["approach"],
        test_plan=data["test_plan"],
        risk_class=data["risk_class"],
        estimated_tokens=result.prompt_tokens + result.completion_tokens,
        latency_seconds=result.latency_seconds,
    )
