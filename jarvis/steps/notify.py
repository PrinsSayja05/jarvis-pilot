"""Send a Telegram notification to the developer: a plan awaiting approval, a finished PR, or a FAILED run.

Scope: one fixed recipient (TELEGRAM_CHAT_ID). The CEO chat (TELEGRAM_CEO_CHAT_ID)
is no longer messaged; smart per-ticket routing (WMCNL-2557) is future work.
"""
from __future__ import annotations

import logging

import httpx

from jarvis.config import JarvisConfig
from jarvis.models.plan import Plan
from jarvis.models.pr_result import PullRequestResult
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket

logger = logging.getLogger("jarvis.notify")


def notify(
    ticket: JiraTicket,
    pr_result: PullRequestResult,
    test_result: TestResult,
    review_result: ReviewResult,
    config: JarvisConfig,
) -> None:
    text = _build_message(ticket, pr_result, test_result, review_result)
    _send(text, config.telegram.bot_token, config.telegram.engineer_chat_id)


_MAX_ERROR_CHARS = 500
EXPECTED_RUN_DURATION = "~60s"


def build_plan_message(ticket: JiraTicket, plan: Plan) -> str:
    """The plan as shown to the developer before approval (Telegram and the Jira comment)."""
    files = ", ".join(fc.path for fc in plan.files_to_change) or "—"
    tests = "; ".join(plan.test_plan) or "—"
    return (
        f"🤖 JARVIS Plan Ready — {ticket.key}\n"
        f"📋 {ticket.summary}\n"
        "\n"
        f"Approach: {plan.approach}\n"
        f"Files: {files}\n"
        f"Tests: {tests}\n"
        f"Risk: {plan.risk_class}\n"
        f"Duration: {EXPECTED_RUN_DURATION}\n"
        "\n"
        "Reply APPROVE or REJECT"
    )


def notify_plan(ticket: JiraTicket, plan: Plan, config: JarvisConfig) -> None:
    _send(build_plan_message(ticket, plan), config.telegram.bot_token, config.telegram.engineer_chat_id)


def notify_failure(ticket_id: str, error: str, config: JarvisConfig) -> None:
    """Tell the developer a run ended in FAILED, with the reason."""
    if len(error) > _MAX_ERROR_CHARS:
        error = error[:_MAX_ERROR_CHARS] + "…"
    text = f"❌ JARVIS FAILED — {ticket_id} — {error} — Manual intervention needed"
    _send(text, config.telegram.bot_token, config.telegram.engineer_chat_id)


def _build_message(
    ticket: JiraTicket,
    pr_result: PullRequestResult,
    test_result: TestResult,
    review_result: ReviewResult,
) -> str:
    return (
        "✅ JARVIS — PR geöffnet\n"
        f"Ticket: {ticket.key} — {ticket.summary}\n"
        f"PR: {pr_result.url}\n"
        f"Tests: {test_result.passed_count} bestanden\n"
        f"Judge: {review_result.findings}"
    )


def _send(text: str, bot_token: str, chat_id: str) -> None:
    """Best effort: a failed notification must never change the outcome of the run."""
    if bot_token in ("", "disabled"):
        logger.warning("telegram notification skipped: TELEGRAM_BOT_TOKEN is not configured")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Log only the status: the exception text contains the URL, and the URL contains the bot token.
        logger.warning("telegram notification failed: HTTP %s", exc.response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("telegram notification failed: %s", type(exc).__name__)
