"""JARVIS's footprint in Jira: plan, result and failure comments, and the in-progress label.

All comments share one format: a coloured panel, a first line "<icon> JARVIS <what>: <ticket>",
then one line per aspect with a fixed icon (🧭 plan, ✅ tests, 🔍 review, 🔗 PR) and the Run-ID.
Failures use the red error panel so they stand out from the blue plan and green result panels.

After a PR the ticket moves to its review status (WMCNL-2553). Projects without a review
status (JW) are logged and skipped; the run does not fail.
"""
from __future__ import annotations

import logging

import httpx

from jarvis.clients.jira_client import IN_PROGRESS_LABEL, JiraClient
from jarvis.config import JarvisConfig
from jarvis.models.plan import Plan
from jarvis.models.pr_result import PullRequestResult
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket
from jarvis.steps.notify import build_plan_message

logger = logging.getLogger("jarvis.jira_comment")


def jira_comment(
    ticket: JiraTicket,
    pr_result: PullRequestResult,
    test_result: TestResult,
    review_result: ReviewResult,
    run_id: str,
    config: JarvisConfig,
    *,
    repairs: int = 0,
) -> None:
    client = JiraClient(config.jira)

    tests = f"{test_result.passed_count} bestanden" if test_result.passed else "fehlgeschlagen"
    if repairs:
        tests += f", nach {repairs} Reparatur" + ("en" if repairs > 1 else "")
    review_icon = "🔍" if review_result.passed else "⚠️"
    text = (
        f"🔗 JARVIS Draft PR: {ticket.key}\n"
        f"🔗 PR: {pr_result.url}\n"
        f"✅ Tests: {tests} (exit_code={test_result.exit_code})\n"
        f"{review_icon} Review ({config.models.reviewer}): {review_result.findings}\n"
        f"Run-ID: {run_id}"
    )
    # green when the judge is happy, yellow when its review failed or found problems
    client.add_comment(ticket.key, text, panel="success" if review_result.passed else "warning")
    try:  # the PR and the comment exist; a status change must never fail the run
        client.transition_to_review(ticket.key)
    except httpx.HTTPError as exc:
        logger.warning("jira status %s: could not move to review: %s", ticket.key, exc)


def jira_plan_comment(ticket: JiraTicket, plan: Plan, run_id: str, config: JarvisConfig) -> None:
    """Post the plan before approval so the developer can read it in Jira too."""
    JiraClient(config.jira).add_comment(
        ticket.key, build_plan_message(ticket, plan) + f"\n\nRun-ID: {run_id}", panel="info"
    )


MANUAL_INTERVENTION = "❌ JARVIS could not complete: manual intervention needed"


def jira_failure_comment(
    ticket_id: str,
    reason: str,
    run_id: str,
    config: JarvisConfig,
    *,
    diff: str | None = None,
) -> None:
    """Tell the ticket that JARVIS gave up. With `diff`, the generated change is attached as a .patch."""
    client = JiraClient(config.jira)
    text = f"{MANUAL_INTERVENTION}\nManueller Eingriff nötig.\nGrund: {reason}\nRun-ID: {run_id}"
    if diff:
        filename = f"jarvis-{ticket_id.lower()}-{run_id[:8]}.patch"
        client.add_attachment(ticket_id, filename, diff.encode("utf-8"))
        text += f"\nDer generierte Diff ist als Anhang {filename} beigefügt."
    client.add_comment(ticket_id, text, panel="error")


def mark_in_progress(ticket_key: str, config: JarvisConfig, active: bool) -> None:
    """Set or clear the jarvis-in-progress label. Best effort: a marker must never fail a run."""
    try:
        JiraClient(config.jira).set_label(ticket_key, IN_PROGRESS_LABEL, active)
    except Exception as exc:
        logger.warning("jira label %s on %s: could not %s it: %s", IN_PROGRESS_LABEL, ticket_key,
                       "set" if active else "remove", exc)
