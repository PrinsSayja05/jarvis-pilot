"""JARVIS scores its own finished run, 1 to 10.

This is not the PR review. The judge in REVIEW asks "is this diff good enough to ship".
This asks "how well did the whole run go", which includes things the diff cannot show: how
many repair attempts it took, whether the judge complained, whether a human rejected the plan.

It runs after NOTIFY, in its own thread, and nothing waits for it. A rating that fails, times
out or comes back malformed is dropped with a log line; the run is already over either way.
The call goes to the reviewer model (laaj-judge), the small one, with a short prompt.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from jarvis.config import JarvisConfig

logger = logging.getLogger("jarvis.self_rating")

MIN_SCORE, MAX_SCORE = 1, 10
_MAX_REASON_CHARS = 300

_SYSTEM = """You rate how well an automated coding run went, from 1 to 10.

10 = the plan matched the ticket, the change worked first time, no repairs, no review warnings,
     and the human approved it as proposed.
5  = it got there, but needed repairs or drew a review warning.
1  = the run failed, or the human rejected the plan because it did not match the ticket.

Weigh these: did the plan address what the ticket actually asked for; how many repair attempts
were needed; did the reviewer warn; did a human reject it.

Answer with JSON only: {"score": <1-10>, "reasoning": "<one sentence, German>",
"plan_matched_ticket": <true|false>}"""


def _facts(ticket_summary: str, ticket_description: str, plan_approach: str, *,
           state: str, dry_run: bool, approved: bool, repairs: int,
           judge_warning: bool, judge_findings: str, tests_passed: int, error: str) -> str:
    lines = [
        f"Ticket: {ticket_summary}",
        f"Ticket description (shortened): {(ticket_description or '')[:600]}",
        f"Plan approach: {(plan_approach or '(no plan)')[:600]}",
        f"Outcome: {state}" + (" (dry run, no code was changed)" if dry_run else ""),
        f"Approved by a human: {'yes' if approved else 'no, rejected or never approved'}",
        f"Repair attempts: {repairs}",
        f"Reviewer warning: {'yes - ' + (judge_findings or '')[:300] if judge_warning else 'no'}",
        f"Tests passed: {tests_passed}",
    ]
    if error:
        lines.append(f"Error: {error[:300]}")
    return "\n".join(lines)


def build_rating(result: Any, ticket: Any, config: JarvisConfig) -> dict | None:
    """One model call. Returns the rating, or None when it could not be produced."""
    from jarvis.clients.litellm_client import LiteLLMClient
    from jarvis.state import RunState

    repairs = result.repair_result.attempts if result.repair_result else 0
    judge_warning = bool(result.review_result and not result.review_result.passed)
    factors = {
        "repairs_needed": repairs,
        "judge_warnings": judge_warning,
        "was_rejected": not bool(result.approved),
        "dry_run": bool(getattr(result, "dry_run", False)),
    }
    user = _facts(
        getattr(ticket, "summary", ""), getattr(ticket, "description", ""),
        result.plan.approach if result.plan else "",
        state=result.state.value if hasattr(result.state, "value") else str(result.state),
        dry_run=factors["dry_run"],
        approved=bool(result.approved),
        repairs=repairs,
        judge_warning=judge_warning,
        judge_findings=result.review_result.findings if result.review_result else "",
        tests_passed=result.test_result.passed_count if result.test_result else 0,
        error=result.error or "",
    )
    # stream=False: the gateway route for the reviewer model returns an empty stream.
    answer = LiteLLMClient(config.litellm).complete(
        model_alias=config.models.reviewer,
        messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        stream=False,
    )
    data = _parse(answer.content)
    if data is None:
        return None
    factors["plan_matched_ticket"] = bool(data.get("plan_matched_ticket", True))
    return {
        "score": data["score"],
        "reasoning": data["reasoning"],
        "factors": factors,
        "model": config.models.reviewer,
        "latency_seconds": round(answer.latency_seconds, 2),
    }


def _parse(content: str) -> dict | None:
    """The reviewer model sometimes wraps its JSON in prose; take the first object it contains."""
    try:
        data = json.loads(content)
    except ValueError:
        match = re.search(r"\{.*\}", content or "", re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    try:
        score = int(round(float(data.get("score"))))
    except (TypeError, ValueError):
        return None
    reason = " ".join(str(data.get("reasoning", "")).split())[:_MAX_REASON_CHARS]
    return {"score": max(MIN_SCORE, min(MAX_SCORE, score)), "reasoning": reason,
            "plan_matched_ticket": bool(data.get("plan_matched_ticket", True))}


def rate_in_background(result: Any, ticket: Any, config: JarvisConfig,
                       on_done=None) -> threading.Thread | None:
    """Fire and forget. The caller never waits and never sees an exception from here."""
    def work() -> None:
        try:
            rating = build_rating(result, ticket, config)
        except Exception as exc:
            logger.info("self-rating for %s not produced (%s)", result.run_id, type(exc).__name__)
            return
        if rating is None:
            logger.info("self-rating for %s not produced (unusable answer)", result.run_id)
            return
        logger.info("self-rating %s: %s/10 (%s)", result.run_id, rating["score"], rating["reasoning"])
        if on_done is not None:
            try:
                on_done(rating)
            except Exception:
                logger.exception("self-rating callback failed")

    thread = threading.Thread(target=work, name=f"self-rating-{result.ticket_id}", daemon=True)
    thread.start()
    return thread
