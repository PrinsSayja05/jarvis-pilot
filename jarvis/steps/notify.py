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

NOT_ASSIGNED = "nicht zugewiesen"
# Where a human goes to act on the message. CONSOLE_URL overrides it per installation.
CONSOLE_URL = os.getenv("CONSOLE_URL", "https://192.168.178.75:9443")
_MAX_APPROACH_SENTENCES = 2
_MAX_APPROACH_CHARS = 240
_MAX_FILES_SHOWN = 4
_MAX_JUDGE_CHARS = 160
_MAX_ERROR_CHARS = 500
EXPECTED_RUN_DURATION = "~60s"
_RISK_DE = {"low": "niedrig", "medium": "mittel", "high": "hoch"}


def _esc(text: str) -> str:
    """Telegram HTML mode: only these three characters need escaping. Markdown is not usable here,
    because a file name like tests/test_app.py would turn into italics on the underscore."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _style(html: bool):
    """(escape, bold) for the target. The same message goes to Telegram as HTML and to a Jira
    comment as plain text, where a <b> tag would simply be shown as characters."""
    if html:
        return _esc, (lambda t: f"<b>{t}</b>")
    return (lambda t: t or ""), (lambda t: t)


def _for_line(assignee_name: str, *, html: bool = False) -> str:
    """Second line of every message: who the ticket belongs to. Never blank, never fails."""
    e, _ = _style(html)
    return f"Für: {e((assignee_name or '').strip() or NOT_ASSIGNED)}"


# run_id -> (chat_id, message_id, text) of the plan message. Keeping the text means the message can
# be rewritten with the decision underneath it without asking Telegram what it used to say.
PLAN_MESSAGES: dict[str, tuple[str, int, str]] = {}


def _short_approach(text: str) -> str:
    """At most two sentences, so the plan still reads at a glance on a phone."""
    text = " ".join((text or "").split())
    short = " ".join(re.split(r"(?<=[.!?])\s+", text)[:_MAX_APPROACH_SENTENCES]).strip()
    if len(short) > _MAX_APPROACH_CHARS:
        short = short[:_MAX_APPROACH_CHARS].rsplit(" ", 1)[0] + " …"
    return short or "—"


def _clip_findings(findings: str, passed: bool) -> str:
    text = " ".join((findings or "").split())
    if not text:
        return "keine Einwände" if passed else "—"
    if len(text) > _MAX_JUDGE_CHARS:
        text = text[:_MAX_JUDGE_CHARS].rsplit(" ", 1)[0] + " …"
    return text


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
    total_tests: int
    duration_seconds: float
    judge_summary: str
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


def build_result_message(item: DoneItem, *, html: bool = False) -> str:
    """One finished run. The first two lines carry the whole story in a notification preview."""
    e, b = _style(html)
    tests = f"{item.passed_count}/{item.total_tests} bestanden" if item.total_tests else f"{item.passed_count} bestanden"
    if item.repairs:
        tests += f" (nach {item.repairs} Reparatur" + ("en)" if item.repairs > 1 else ")")
    return "\n".join([
        f"✅ {b('J.A.R.V.I.S. fertig')} — {e(item.key)}",
        _for_line(item.assignee_name, html=html),
        "",
        f"Tests: {e(tests)}   🔍 {e(item.judge_summary or 'keine Einwände')}",
        f"🔗 {e(item.pr_url)}",
        f"⏱️ {item.duration_seconds:.0f}s",
    ])


def build_batch_message(items: list[DoneItem], *, html: bool = False) -> str:
    """One run gets the full result message; several runs of one person get a combined one."""
    if len(items) == 1:
        return build_result_message(items[0], html=html)
    e, b = _style(html)
    lines = [f"✅ {b('J.A.R.V.I.S. fertig')} — {len(items)} Tickets",
             _for_line(items[0].assignee_name, html=html), ""]
    for i in items:
        extras = (f" ({i.repairs} Reparatur" + ("en)" if i.repairs > 1 else ")")) if i.repairs else ""
        warn = "  ⚠️ Judge-Warnung" if i.judge_warning else ""
        lines.append(f"{e(i.key)} · PR {i.pr_number}{e(extras)}{warn}")
        lines.append(f"🔗 {e(i.pr_url)}")
    return "\n".join(lines)


# ---- channels ---------------------------------------------------------------------------------

Channel = Callable[[list[DoneItem], JarvisConfig], None]


def _telegram_channel(items: list[DoneItem], config: JarvisConfig) -> None:
    _send(build_batch_message(items, html=True), config.telegram.bot_token, config.telegram.engineer_chat_id)


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
    duration_seconds: float = 0.0,
) -> None:
    """Queue a finished run; it is sent together with other runs of the same assignee."""
    _batcher.add(
        DoneItem(
            key=ticket.key,
            summary=ticket.summary,
            url=ticket.url,
            pr_url=pr_result.url,
            passed_count=test_result.passed_count,
            total_tests=test_result.passed_count + test_result.failed + test_result.errors,
            duration_seconds=duration_seconds,
            judge_summary=_clip_findings(review_result.findings, review_result.passed),
            repairs=repairs,
            judge_warning=not review_result.passed,
            assignee_id=ticket.assignee_id,
            assignee_name=ticket.assignee_name,
        ),
        config,
    )


# ---- immediate messages -----------------------------------------------------------------------

def build_plan_message(ticket: JiraTicket, plan: Plan, *, html: bool = False) -> str:
    """The plan as shown to the developer before approval (Telegram and the Jira comment)."""
    paths = [fc.path for fc in plan.files_to_change]
    shown = ", ".join(paths[:_MAX_FILES_SHOWN]) or "keine"
    if len(paths) > _MAX_FILES_SHOWN:
        shown += f" (+{len(paths) - _MAX_FILES_SHOWN} weitere)"
    e, b = _style(html)
    return "\n".join([
        f"🤖 {b('J.A.R.V.I.S.')} — Ticket {e(ticket.key)}",
        _for_line(ticket.assignee_name, html=html),
        "",
        e(ticket.summary),
        "",
        f"Ansatz: {e(_short_approach(plan.approach))}",
        "",
        f"📁 {e(shown)}   ⚠️ Risiko: {e(plan.risk_class)}   ⏱️ {EXPECTED_RUN_DURATION}",
    ])


APPROVE_DATA = "jv:approve:"
REJECT_DATA = "jv:reject:"


def plan_keyboard(run_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Freigeben", "callback_data": APPROVE_DATA + run_id},
        {"text": "❌ Ablehnen", "callback_data": REJECT_DATA + run_id},
    ]]}


def notify_plan(ticket: JiraTicket, plan: Plan, config: JarvisConfig, run_id: str = "") -> None:
    """With a run_id the message carries Freigeben and Ablehnen buttons, and a press acts on that
    console run. A run started in the terminal has no id here and therefore gets no buttons."""
    text = build_plan_message(ticket, plan, html=True)
    message_id = _send(text, config.telegram.bot_token, config.telegram.engineer_chat_id,
                       reply_markup=plan_keyboard(run_id) if run_id else None)
    if run_id and message_id:
        PLAN_MESSAGES[run_id] = (config.telegram.engineer_chat_id, message_id, text)


def build_failure_message(ticket_id: str, error: str, assignee_name: str = "", ticket_url: str = "",
                          *, html: bool = False) -> str:
    error = " ".join((error or "").split())
    if len(error) > _MAX_ERROR_CHARS:
        error = error[:_MAX_ERROR_CHARS] + "…"
    e, b = _style(html)
    return "\n".join([
        f"❌ {b('J.A.R.V.I.S. — manueller Eingriff nötig')} — {e(ticket_id)}",
        _for_line(assignee_name, html=html),
        "",
        f"Grund: {e(error)}",
        f"🔗 {e(ticket_url or CONSOLE_URL)}",
    ])


def notify_failure(ticket_id: str, error: str, config: JarvisConfig, *,
                   assignee_name: str = "", ticket_url: str = "") -> None:
    """Tell the developer a run ended in FAILED, with the reason. Never batched."""
    _send(build_failure_message(ticket_id, error, assignee_name, ticket_url, html=True),
          config.telegram.bot_token, config.telegram.engineer_chat_id)


def _send(text: str, bot_token: str, chat_id: str, *, reply_markup: dict | None = None) -> int | None:
    """Best effort: a failed notification must never change the outcome of the run.

    Returns the Telegram message id, which is what later lets a plan message be edited.
    """
    if bot_token in ("", "disabled"):
        logger.warning("telegram notification skipped: TELEGRAM_BOT_TOKEN is not configured")
        return None
    body: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
    if reply_markup:
        body["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(url, json=body)
        response.raise_for_status()
        return response.json().get("result", {}).get("message_id")
    except httpx.HTTPStatusError as exc:
        # Log only the status: the exception text contains the URL, and the URL contains the bot token.
        logger.warning("telegram notification failed: HTTP %s", exc.response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("telegram notification failed: %s", type(exc).__name__)
    return None
