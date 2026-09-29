"""Post a PR summary comment on the Jira ticket and advance its status.

The status move (e.g. -> "In Review") covers WMCNL-2553 without needing a
separate module - it is just another Jira REST call alongside the comment.
"""
from __future__ import annotations

from jarvis.clients.jira_client import JiraClient
from jarvis.config import JarvisConfig
from jarvis.models.plan import Plan
from jarvis.models.pr_result import PullRequestResult
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket
from jarvis.steps.notify import build_plan_message


def jira_comment(
    ticket: JiraTicket,
    pr_result: PullRequestResult,
    test_result: TestResult,
    review_result: ReviewResult,
    run_id: str,
    config: JarvisConfig,
) -> None:
    client = JiraClient(config.jira)

    text = (
        f"JARVIS hat einen Draft PR geöffnet: {pr_result.url}\n"
        f"Tests: {'bestanden' if test_result.passed else 'fehlgeschlagen'} "
        f"(exit_code={test_result.exit_code})\n"
        f"Judge ({config.models.reviewer}): {review_result.findings}\n"
        f"Run-ID: {run_id}"
    )
    client.add_comment(ticket.key, text)
    client.transition_to_next(ticket.key)


def jira_plan_comment(ticket: JiraTicket, plan: Plan, run_id: str, config: JarvisConfig) -> None:
    """Post the plan before approval so the developer can read it in Jira too."""
    JiraClient(config.jira).add_comment(ticket.key, build_plan_message(ticket, plan) + f"\n\nRun-ID: {run_id}")


MANUAL_INTERVENTION = "JARVIS could not complete — manual intervention needed"


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
    text = f"{MANUAL_INTERVENTION}\nGrund: {reason}\nRun-ID: {run_id}"
    if diff:
        filename = f"jarvis-{ticket_id.lower()}-{run_id[:8]}.patch"
        client.add_attachment(ticket_id, filename, diff.encode("utf-8"))
        text += f"\nDer generierte Diff ist als Anhang {filename} beigefügt."
    client.add_comment(ticket_id, text)
