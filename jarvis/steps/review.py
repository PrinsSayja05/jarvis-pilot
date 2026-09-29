"""Call jarvis-review (laaj-judge:v2) to independently review the diff.

Uses a different model from the one that wrote the code - fewer correlated
mistakes than having the coder grade its own work.
"""
from __future__ import annotations

import json

from jarvis.clients.litellm_client import LiteLLMClient
from jarvis.config import JarvisConfig
from jarvis.models.code_change import CodeChange
from jarvis.models.plan import Plan
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket

_SYSTEM_PROMPT = """You are an independent code reviewer for an approved software change.
Check: correctness, security issues, missing tests, and whether the diff stays within the
approved plan's scope. Respond in German.
Return JSON only: {"passed": bool, "findings": "<short summary in German>"}"""


def review(
    ticket: JiraTicket,
    plan: Plan,
    change: CodeChange,
    test_result: TestResult,
    config: JarvisConfig,
) -> ReviewResult:
    client = LiteLLMClient(config.litellm)

    user_prompt = (
        f"Ticket {ticket.key}: {ticket.summary}\n\n"
        f"Approved approach: {plan.approach}\n\n"
        f"Diff:\n{change.diff}\n\n"
        f"Tests: {'passed' if test_result.passed else 'FAILED'} "
        f"(exit_code={test_result.exit_code})\n"
        f"stdout:\n{test_result.stdout}\nstderr:\n{test_result.stderr}"
    )

    result = client.complete(
        model_alias=config.models.reviewer,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        stream=False,  # the jarvis-review route returns an empty stream
    )

    data = json.loads(result.content)
    return ReviewResult(
        passed=bool(data["passed"]),
        findings=data.get("findings", ""),
        latency_seconds=result.latency_seconds,
    )
