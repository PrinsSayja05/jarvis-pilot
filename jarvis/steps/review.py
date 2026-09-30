"""Call jarvis-review (laaj-judge:v2) to independently review the diff.

Uses a different model from the one that wrote the code: fewer correlated mistakes than
having the coder grade its own work.

Context: the reviewer reads at most 8.192 tokens, while the coder may write diffs up to
65.536. Sending a large diff whole means the model silently sees only part of it and still
answers "keine Einwände", which reads like a full review and is not one. So:

- a diff inside the budget is reviewed in one pass, exactly as before;
- a larger diff is split at file boundaries into blocks that each fit, every block is
  reviewed, and the findings are combined;
- a single file too large to fit is reviewed as far as it fits and the result says so, in
  German, with the line counts. "Teilweise geprüft" never gets rounded up to a clean review.

A block that fails is a finding, not a crash: the run already has passing tests, and a
broken reviewer must not throw away a good change.
"""
from __future__ import annotations

import json
import logging
import re

from jarvis.clients.litellm_client import LiteLLMClient
from jarvis.config import JarvisConfig
from jarvis.models.code_change import CodeChange
from jarvis.models.plan import Plan
from jarvis.models.review import ReviewResult
from jarvis.models.test_result import TestResult
from jarvis.models.ticket import JiraTicket

logger = logging.getLogger("jarvis.review")

_SYSTEM_PROMPT = """You are an independent code reviewer for an approved software change.
Check: correctness, security issues, missing tests, and whether the diff stays within the
approved plan's scope. Respond in German.
Return JSON only: {"passed": bool, "findings": "<short summary in German>"}"""

_BLOCK_NOTE = """

You are reviewing part {index} of {total} of a larger diff. Judge only what you see here.
Do not complain that context is missing; another part may contain it."""

# The reviewer route (laaj-judge:v2) takes 8192 tokens in total. The rest of the prompt
# (instruction, ticket, plan, test output) needs room too, so the diff gets this much.
REVIEW_CONTEXT_TOKENS = 8192
DIFF_TOKEN_BUDGET = 5000
# Measured against the real model on 30.09.2026: a dense diff (identifiers with digits and
# punctuation) came out at about 1.7 characters per token, far denser than prose. 2.0 is the
# working assumption, and the estimate is only a hint: if the model still says the prompt is too
# long, the block is shrunk and retried, so a wrong guess costs a retry and never a silent pass.
CHARS_PER_TOKEN = 2.0
_SHRINK_TO = 0.6      # how much of a too-large block to keep on a retry
_MAX_SHRINKS = 3
_MAX_TEST_OUTPUT_CHARS = 2000
_MAX_BLOCKS = 6  # beyond this the review costs more than it is worth; the rest is reported as unchecked


def estimate_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def budget_chars() -> int:
    return int(DIFF_TOKEN_BUDGET * CHARS_PER_TOKEN)


def split_diff(diff: str, limit_chars: int | None = None) -> list[str]:
    """Split at file boundaries into blocks that each fit the budget.

    A single file larger than the budget is not cut mid-hunk: it is returned whole and the
    caller decides what to do, because half a hunk reviews worse than an honest partial note.
    """
    limit = limit_chars or budget_chars()
    if len(diff) <= limit:
        return [diff]
    # keep each "--- a/file" header with its hunks
    parts = re.split(r"(?m)^(?=--- )", diff)
    parts = [p for p in parts if p.strip()]
    if len(parts) <= 1:
        return [diff]

    blocks: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) > limit:
            blocks.append(current)
            current = part
        else:
            current += part
    if current:
        blocks.append(current)
    return blocks


def _count_lines(diff: str) -> int:
    """Changed lines, the number a human recognises from the PR."""
    return sum(1 for line in (diff or "").splitlines()
               if (line.startswith("+") or line.startswith("-"))
               and not line.startswith(("+++", "---")))


class ContextTooLarge(Exception):
    """The model refused the prompt as too long, whatever our estimate said."""


def _is_context_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "contextwindowexceeded" in text or "exceeds the available context" in text or "exceed_context_size" in text


def _ask_shrinking(client, config, ticket, plan, test_result, diff_part, note):
    """Ask, and if the model says the prompt is too long, keep less of the diff and ask again.

    Returns (passed, findings, latency, text_actually_reviewed) so the caller can report how
    much was really seen instead of assuming the whole block was.
    """
    text = diff_part
    for attempt in range(_MAX_SHRINKS + 1):
        try:
            passed, findings, latency = _ask(client, config, ticket, plan, test_result, text, note)
            return passed, findings, latency, text
        except Exception as exc:
            if not _is_context_error(exc) or attempt == _MAX_SHRINKS:
                raise
            cut = max(1000, int(len(text) * _SHRINK_TO))
            logger.warning("review: model refused %d chars as too long, retrying with %d", len(text), cut)
            text = text[:cut]
    raise ContextTooLarge("unreachable")


def _ask(client, config, ticket, plan, test_result, diff_part, note) -> tuple[bool, str, float]:
    user_prompt = (
        f"Ticket {ticket.key}: {ticket.summary}\n\n"
        f"Approved approach: {plan.approach}\n\n"
        f"Diff:\n{diff_part}\n\n"
        f"Tests: {'passed' if test_result.passed else 'FAILED'} "
        f"(exit_code={test_result.exit_code})\n"
        f"stdout:\n{(test_result.stdout or '')[:_MAX_TEST_OUTPUT_CHARS]}\n"
        f"stderr:\n{(test_result.stderr or '')[:_MAX_TEST_OUTPUT_CHARS]}"
    )
    result = client.complete(
        model_alias=config.models.reviewer,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT + note},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        stream=False,  # the jarvis-review route returns an empty stream
    )
    data = json.loads(result.content)
    return bool(data["passed"]), str(data.get("findings", "")), result.latency_seconds


def review(
    ticket: JiraTicket,
    plan: Plan,
    change: CodeChange,
    test_result: TestResult,
    config: JarvisConfig,
    on_note=None,
) -> ReviewResult:
    """`on_note` receives one German line saying how the diff was reviewed, for the run log."""
    note = on_note or (lambda message: None)
    client = LiteLLMClient(config.litellm)
    diff = change.diff or ""
    total_lines = _count_lines(diff)
    tokens = estimate_tokens(diff)

    if tokens <= DIFF_TOKEN_BUDGET:
        passed, findings, latency, seen = _ask_shrinking(client, config, ticket, plan, test_result, diff, "")
        if seen == diff:
            logger.info("review: single pass, ~%d tokens of %d budget", tokens, DIFF_TOKEN_BUDGET)
            note(f"Prüfung: vollständig, {total_lines} geänderte Zeilen in einem Durchgang")
            return ReviewResult(passed=passed, findings=findings, latency_seconds=latency)
        checked = _count_lines(seen)   # the model wanted less than we estimated
        note(f"Prüfung: nur teilweise, {checked} von {total_lines} geänderten Zeilen")
        return ReviewResult(
            passed=passed,
            findings=f"⚠️ Teilweise geprüft ({checked} von {total_lines} Zeilen): {findings}",
            latency_seconds=latency,
        )

    blocks = split_diff(diff)
    if len(blocks) == 1:
        # one file bigger than the budget: review the part that fits and say so
        cut = budget_chars()
        shown = diff[:cut]
        checked = _count_lines(shown)
        passed, findings, latency, seen = _ask_shrinking(
            client, config, ticket, plan, test_result, shown,
            "\n\nYou are seeing only the beginning of a larger diff that could not be split.",
        )
        checked = _count_lines(seen)
        logger.warning("review: partial, %d of %d changed lines seen (single oversized file)",
                       checked, total_lines)
        note(f"Prüfung: nur teilweise, {checked} von {total_lines} geänderten Zeilen "
             f"(eine einzelne Datei ist größer als das Kontextfenster)")
        return ReviewResult(
            passed=passed,
            findings=(f"⚠️ Teilweise geprüft ({checked} von {total_lines} Zeilen): der Prüfer konnte nur "
                      f"den Anfang der Änderung lesen. {findings}"),
            latency_seconds=latency,
        )

    unchecked = blocks[_MAX_BLOCKS:]
    blocks = blocks[:_MAX_BLOCKS]
    logger.info("review: split into %d block(s), ~%d tokens total", len(blocks), tokens)
    note(f"Prüfung: aufgeteilt in {len(blocks)} Blöcke, {total_lines} geänderte Zeilen insgesamt")

    passed_all, parts, latency_total, checked_lines = True, [], 0.0, 0
    for index, block in enumerate(blocks, 1):
        try:
            passed, findings, latency, seen = _ask_shrinking(
                client, config, ticket, plan, test_result, block,
                _BLOCK_NOTE.format(index=index, total=len(blocks)),
            )
        except Exception as exc:  # one bad block must not discard the whole review
            logger.warning("review: block %d failed (%s)", index, type(exc).__name__)
            parts.append(f"Block {index}: nicht geprüft ({type(exc).__name__})")
            passed_all = False
            continue
        latency_total += latency
        checked_lines += _count_lines(seen)
        passed_all = passed_all and passed
        parts.append(f"Block {index}: {findings.strip()}" if findings.strip() else f"Block {index}: ohne Befund")

    if checked_lines < total_lines and not unchecked:
        passed_all = False
    head = (f"Geprüft in {len(blocks)} Blöcken ({checked_lines} von {total_lines} geänderten Zeilen)."
            if checked_lines >= total_lines else
            f"⚠️ Teilweise geprüft: {checked_lines} von {total_lines} geänderten Zeilen in "
            f"{len(blocks)} Blöcken.")
    if unchecked:
        rest = sum(_count_lines(b) for b in unchecked)
        head = (f"⚠️ Teilweise geprüft: {checked_lines} von {total_lines} geänderten Zeilen in "
                f"{len(blocks)} Blöcken, {rest} Zeilen wurden nicht geprüft.")
        passed_all = False
        note(f"Prüfung: {rest} Zeilen ungeprüft, mehr als {_MAX_BLOCKS} Blöcke")
    return ReviewResult(
        passed=passed_all,
        findings=head + " " + " | ".join(parts),
        latency_seconds=latency_total,
    )
