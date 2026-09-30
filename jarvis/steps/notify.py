"""Tell the developer about a run: a plan awaiting approval, finished PRs (batched), or a FAILED run.

Scope: one fixed Telegram recipient (TELEGRAM_CHAT_ID). The CEO chat (TELEGRAM_CEO_CHAT_ID)
is no longer messaged; smart per-ticket routing (WMCNL-2557) is future work.

Finished runs are not sent one by one. They are collected per assignee for a short debounce
window (NOTIFY_BATCH_SECONDS, default 60) and then sent as one message, for example
"✅ JARVIS: 3 Tickets fertig: JW-1 (PR #5), JW-26 (PR #6, 1 Reparatur), JW-18 (PR #7, Judge-Warnung)".
A single run is sent as soon as its window closes. Plans and failures stay immediate:
a plan waits for a human, a failure needs one.

Channels are pluggable: Telegram (skipped while TELEGRAM_BOT_TOKEN is disabled) and a Jira
summary comment. The Jira summary is only posted for two or more tickets, because every
ticket already carries its own result comment; it mentions the assignee so Jira notifies them once.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from jarvis.config import JarvisConfig
from jarvis.models.plan import Plan
from jarvis.models.pr_result import PullRequestResult
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket

logger = logging.getLogger("jarvis.notify")

_MAX_ERROR_CHARS = 500
EXPECTED_RUN_DURATION = "~60s"
_RISK_DE = {"low": "niedrig", "medium": "mittel", "high": "hoch"}


def _batch_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("NOTIFY_BATCH_SECONDS", "60")))
    except ValueError:
        return 60.0


# A steady stream of runs must not hold messages back forever: a batch is sent at the latest after this.
_MAX_HOLD_FACTOR = 3


@dataclass
class DoneItem:
    """One finished run, as it appears in a batch message."""
    key: str
    summary: str
    url: str          # Jira browse URL
    pr_url: str
    passed_count: int
    repairs: int
    judge_warning: bool
    assignee_id: str
    assignee_name: str

    @property
    def pr_number(self) -> str:
        match = re.search(r"/pull/(\d+)", self.pr_url or "")
        return f"#{match.group(1)}" if match else "?"

    def short(self) -> str:
        extras = [f"PR {self.pr_number}"]
        if self.repairs:
            extras.append(f"{self.repairs} Reparatur" + ("en" if self.repairs > 1 else ""))
        if self.judge_warning:
            extras.append("Judge-Warnung")
        return f"{self.key} ({', '.join(extras)})"


def build_batch_message(items: list[DoneItem]) -> str:
    if len(items) == 1:
        head = f"✅ JARVIS: 1 Ticket fertig: {items[0].short()}"
    else:
        head = f"✅ JARVIS: {len(items)} Tickets fertig: " + ", ".join(i.short() for i in items)
    lines = [head, ""]
    for i in items:
        lines.append(f"🔗 {i.key}: {i.pr_url}  ({i.passed_count} Tests bestanden)")
    return "\n".join(lines)


# ---- channels ---------------------------------------------------------------------------------

Channel = Callable[[list[DoneItem], JarvisConfig], None]


def _telegram_channel(items: list[DoneItem], config: JarvisConfig) -> None:
    _send(build_batch_message(items), config.telegram.bot_token, config.telegram.engineer_chat_id)


def _jira_channel(items: list[DoneItem], config: JarvisConfig) -> None:
    if len(items) < 2:  # the ticket already has its own result comment
        return
    from jarvis.clients.jira_client import JiraClient  # late: jira_comment imports this module

    last = items[-1]  # the ticket that finished last, where the assignee is most likely looking
    JiraClient(config.jira).add_comment(
        last.key, build_batch_message(items), panel="success", mention=last.assignee_id or None
    )


CHANNELS: list[Channel] = [_telegram_channel, _jira_channel]


# ---- batching ---------------------------------------------------------------------------------

class _Batcher:
    """Collects finished runs per assignee and flushes each group once it has been quiet for `window` seconds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: dict[str, dict] = {}  # assignee -> {"items", "config", "first", "timer"}

    def add(self, item: DoneItem, config: JarvisConfig) -> None:
        window = _batch_seconds()
        if window == 0:
            self._deliver([item], config)
            return
        who = item.assignee_id or "unassigned"
        with self._lock:
            group = self._groups.setdefault(who, {"items": [], "config": config, "first": time.monotonic(), "timer": None})
            group["items"].append(item)
            group["config"] = config
            if group["timer"] is not None:
                group["timer"].cancel()
            # Debounce: wait for a quiet window, but never longer than the hold cap since the first item.
            left = window * _MAX_HOLD_FACTOR - (time.monotonic() - group["first"])
            timer = threading.Timer(max(0.0, min(window, left)), self.flush, args=(who,))
            timer.daemon = True
            group["timer"] = timer
            timer.start()
        logger.info("notify: %s queued for %s (%d in batch, window %.0fs)", item.key, who, len(group["items"]), window)

    def flush(self, who: str | None = None) -> None:
        """Send one group, or every group (who=None, used at process exit)."""
        with self._lock:
            keys = list(self._groups) if who is None else [who]
            groups = [self._groups.pop(k) for k in keys if k in self._groups]
        for group in groups:
            if group["timer"] is not None:
                group["timer"].cancel()
            self._deliver(group["items"], group["config"])

    def pending(self) -> int:
        with self._lock:
            return sum(len(g["items"]) for g in self._groups.values())

    @staticmethod
    def _deliver(items: list[DoneItem], config: JarvisConfig) -> None:
        logger.info("notify: sending batch of %d: %s", len(items), ", ".join(i.key for i in items))
        for channel in CHANNELS:
            try:  # best effort per channel: one broken channel must not silence the others
                channel(items, config)
            except Exception:
                logger.exception("notify channel %s failed", getattr(channel, "__name__", channel))


_batcher = _Batcher()
atexit.register(_batcher.flush)  # a CLI run ends the process right after NOTIFY: send, don't drop


def flush_pending() -> None:
    _batcher.flush()


def notify(
    ticket: JiraTicket,
    pr_result: PullRequestResult,
    test_result: TestResult,
    review_result: ReviewResult,
    config: JarvisConfig,
    *,
    repairs: int = 0,
) -> None:
    """Queue a finished run; it is sent together with other runs of the same assignee."""
    _batcher.add(
        DoneItem(
            key=ticket.key,
            summary=ticket.summary,
            url=ticket.url,
            pr_url=pr_result.url,
            passed_count=test_result.passed_count,
            repairs=repairs,
            judge_warning=not review_result.passed,
            assignee_id=ticket.assignee_id,
            assignee_name=ticket.assignee_name,
        ),
        config,
    )


# ---- immediate messages -----------------------------------------------------------------------

def build_plan_message(ticket: JiraTicket, plan: Plan) -> str:
    """The plan as shown to the developer before approval (Telegram and the Jira comment)."""
    files = ", ".join(fc.path for fc in plan.files_to_change) or "keine"
    tests = "; ".join(plan.test_plan) or "keine"
    return (
        f"🧭 JARVIS Plan: {ticket.key}\n"
        f"📋 {ticket.summary}\n"
        "\n"
        f"Ansatz: {plan.approach}\n"
        f"Dateien: {files}\n"
        f"✅ Geplante Tests: {tests}\n"
        f"Risiko: {_RISK_DE.get(plan.risk_class, plan.risk_class)} ({plan.risk_class})\n"
        f"Dauer: {EXPECTED_RUN_DURATION}\n"
        "\n"
        "Freigabe in der JARVIS-Konsole: APPROVE oder REJECT"
    )


def notify_plan(ticket: JiraTicket, plan: Plan, config: JarvisConfig) -> None:
    _send(build_plan_message(ticket, plan), config.telegram.bot_token, config.telegram.engineer_chat_id)


def notify_failure(ticket_id: str, error: str, config: JarvisConfig) -> None:
    """Tell the developer a run ended in FAILED, with the reason. Never batched."""
    if len(error) > _MAX_ERROR_CHARS:
        error = error[:_MAX_ERROR_CHARS] + "…"
    text = f"❌ JARVIS FAILED: {ticket_id}\nGrund: {error}\nManueller Eingriff nötig."
    _send(text, config.telegram.bot_token, config.telegram.engineer_chat_id)


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
