"""The JARVIS run pipeline, shared by the CLI and the web console.

Approval is a callback so each front end can ask a human its own way
(terminal prompt, or the console's Approve/Reject buttons). It is never skipped.
"""
from __future__ import annotations

import getpass
import json
import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from jarvis.clients.minio_client import MinioClient
from jarvis.config import JarvisConfig
from jarvis.models.plan import Plan
from jarvis.audit import record_feedback
from jarvis.models.review import ReviewResult
from jarvis.models.ticket import priority_label
from jarvis.models.run import RunResult
from jarvis.progress import ProgressTracker
from jarvis.state import STATE_LABELS_DE, RunState, StateMachine
from jarvis.steps.code_change import code_change as generate_code_change
from jarvis.steps.create_pr import create_pr
from jarvis.steps.find_repo import choose_repo
from jarvis.steps.jira_comment import jira_comment, jira_failure_comment, jira_plan_comment, mark_in_progress
from jarvis.steps.notify import notify, notify_failure, notify_plan
from jarvis.steps.plan import create_plan
from jarvis.steps.read_repo import read_repo, remove_clone
from jarvis.steps.read_ticket import read_ticket
from jarvis.steps.repair import repair
from jarvis.steps.review import review
from jarvis.steps.run_tests import run_tests

logger = logging.getLogger("jarvis.pipeline")

# Called with (plan, ticket_id); blocks until a human decides; True = approved.
ApproveFn = Callable[[Plan, str], bool]

MAX_CODE_CHANGE_REPAIRS = 3


class ManualInterventionNeeded(Exception):
    """JARVIS used up its fallbacks. `diff`, if set, is attached to the Jira comment."""

    def __init__(self, reason: str, diff: str | None = None) -> None:
        super().__init__(reason)
        self.diff = diff


def execute_run(
    ticket_id: str,
    config: JarvisConfig,
    *,
    dry_run: bool,
    approve: ApproveFn,
    run_id: str | None = None,
    tracker: ProgressTracker | None = None,
    auto_approved: bool = False,
    narrate: Callable[[str], None] | None = None,
) -> RunResult:
    """Runs the whole flow. Never raises for a failed run: the result carries state=FAILED + error.

    `narrate` receives short German status lines to speak (voice / demo mode); it must not block.
    """
    say = _safe_narrator(narrate)
    run_id = run_id or str(uuid.uuid4())
    tracker = tracker or ProgressTracker(run_id, ticket_id)
    machine = StateMachine(run_id=run_id, tracker=tracker)
    result = RunResult(
        run_id=run_id,
        ticket_id=ticket_id,
        state=machine.state,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    repo_map = None
    marked = None  # ticket key carrying the jarvis-in-progress label, removed in `finally`
    who = ("", "")  # (assignee name, ticket URL) for the failure message; empty until the ticket is read
    try:
        machine.transition(RunState.READ_TICKET)
        ticket = read_ticket(ticket_id, config)
        who = (ticket.assignee_name, ticket.url)
        logger.info("ticket %s priority=%s", ticket.key, ticket.priority or "-")
        tracker.note(f"Priorität: {priority_label(ticket.priority) or 'nicht gesetzt'}")
        say(f"Ich habe das Ticket {ticket.key} gelesen: {ticket.summary}. Ich erstelle jetzt einen Plan.")

        machine.transition(RunState.FIND_REPO)
        repo_full_name, repo_reason = choose_repo(ticket, config)
        logger.info("find_repo ticket=%s repo=%s reason=%s", ticket.key, repo_full_name, repo_reason)
        tracker.note(f"repo: {repo_full_name} ({repo_reason})")

        machine.transition(RunState.READ_REPO)
        repo_map = read_repo(repo_full_name, config)

        machine.transition(RunState.PLAN)
        if not dry_run:  # visible in Jira from here on; dry runs never write to Jira
            mark_in_progress(ticket.key, config, True)
            marked = ticket.key
        plan = create_plan(ticket, repo_map, config)
        result.plan = plan
        _announce_plan(ticket, plan, run_id, config, tracker, post_to_jira=not dry_run)
        say(f"Plan ist fertig. {_first_sentence(plan.approach)} Risiko {_RISK_DE.get(plan.risk_class, plan.risk_class)}.")

        machine.transition(RunState.AWAIT_APPROVAL)
        approved = approve(plan, ticket_id)
        # No Keycloak login yet (WMCNL-2533): the approver identity is the OS user running JARVIS.
        approver = getpass.getuser() + (" (auto-approve, testing only)" if auto_approved else "")

        if not approved:
            machine.transition(RunState.CANCELLED)
            result.approved = False
            say("Plan abgelehnt. Keine Änderungen vorgenommen.")
        elif dry_run:
            result.approved = True
            result.approver = approver
            machine.transition(RunState.DONE)
            say("Probelauf abgeschlossen. Der Plan ist freigegeben, es wurde kein Code geändert.")
        else:
            result.approved = True
            result.approver = approver
            _code_test_review_pr(
                ticket, plan, repo_full_name, repo_map, config, machine, tracker, result, approver, run_id, say
            )
            machine.transition(RunState.DONE)

    except Exception as exc:  # top-level run boundary: log, mark FAILED, tell a human
        failed_step = STATE_LABELS_DE.get(machine.state, machine.state.value)
        say(f"Fehler aufgetreten im Schritt {failed_step}. Manueller Eingriff erforderlich.")
        machine.fail(exc)
        result.error = str(exc)
        if isinstance(exc, ManualInterventionNeeded):
            logger.error("Run failed: %s", exc)
        else:
            logger.exception("Run failed")
        # Only a full run that was approved may write to Jira; dry runs never do.
        _report_failure(ticket_id, exc, run_id, config, tracker, who,
                        comment_on_jira=result.approved and not dry_run)
    finally:
        result.state = machine.state
        result.ended_at = datetime.now(timezone.utc).isoformat()
        if repo_map is not None:  # the clone is only needed during the run
            remove_clone(repo_map.local_path)
        if marked:  # whatever the outcome: done, cancelled or failed
            mark_in_progress(marked, config, False)

    return result


_RISK_DE = {"low": "niedrig", "medium": "mittel", "high": "hoch"}


def _safe_narrator(narrate: Callable[[str], None] | None) -> Callable[[str], None]:
    """Speaking is a nice-to-have: it must never change the outcome of a run."""
    def say(text: str) -> None:
        if narrate is None:
            return
        try:
            narrate(text)
        except Exception:
            logger.exception("narration failed")
    return say


def _first_sentence(text: str, limit: int = 200) -> str:
    sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit].rsplit(" ", 1)[0] + "."


def _pr_label(url: str) -> str:
    match = re.search(r"/pull/(\d+)", url or "")
    return f"Nummer {match.group(1)}" if match else ""


def _announce_plan(ticket, plan, run_id, config, tracker, *, post_to_jira: bool) -> None:
    """Show the plan outside the terminal before approval. Best effort: never blocks the run."""
    try:
        notify_plan(ticket, plan, config)
    except Exception:
        logger.exception("could not send the plan notification")
    if post_to_jira:  # dry runs never write to Jira
        try:
            jira_plan_comment(ticket, plan, run_id, config)
            tracker.note(f"Jira: plan posted as a comment on {ticket.key}")
        except Exception:
            logger.exception("could not post the plan comment on %s", ticket.key)


def _report_failure(ticket_id, exc, run_id, config, tracker, who=("", ""), *, comment_on_jira: bool) -> None:
    """Best effort: reporting a failure must never raise out of the run boundary."""
    if comment_on_jira:
        try:
            jira_failure_comment(ticket_id, str(exc), run_id, config, diff=getattr(exc, "diff", None))
            tracker.note(f"Jira: manual-intervention comment posted on {ticket_id}")
        except Exception:
            logger.exception("could not post the failure comment on %s", ticket_id)
    try:
        notify_failure(ticket_id, str(exc), config, assignee_name=who[0], ticket_url=who[1])
    except Exception:
        logger.exception("could not send the failure notification")


def _repair_code_change(ticket, plan, repo_map, config, tracker, error: Exception):
    """CODE_CHANGE produced no usable diff: retry with the error as feedback."""
    for attempt in range(1, MAX_CODE_CHANGE_REPAIRS + 1):
        tracker.note(f"code change repair attempt {attempt}/{MAX_CODE_CHANGE_REPAIRS}")
        try:
            return generate_code_change(
                ticket, plan, repo_map, config, feedback=f"The previous attempt produced no usable diff: {error}"
            )
        except Exception as exc:
            error = exc
            tracker.note(f"code change repair attempt {attempt}: {exc}")
    raise ManualInterventionNeeded(
        f"code change failed after {MAX_CODE_CHANGE_REPAIRS} repair attempts: {error}"
    ) from error


def _code_test_review_pr(
    ticket, plan, repo_full_name, repo_map, config, machine, tracker, result, approver, run_id, say
) -> None:
    machine.transition(RunState.CODE_CHANGE)
    say("Ich ändere jetzt den Code.")
    try:
        change = generate_code_change(ticket, plan, repo_map, config)
    except Exception as exc:
        tracker.note(f"code change failed: {exc}")
        machine.transition(RunState.REPAIR)
        change = _repair_code_change(ticket, plan, repo_map, config, tracker, exc)
    say("Code geändert. Ich starte die Tests.")

    machine.transition(RunState.RUN_TESTS)
    test_result = run_tests(change, repo_map, config)

    if not test_result.passed:
        say(f"Tests fehlgeschlagen: {test_result.failed} Fehler. Ich versuche eine Reparatur.")
        machine.transition(RunState.REPAIR)
        repair_result = repair(ticket, plan, repo_map, change, test_result, config, on_attempt=tracker.note)
        result.repair_result = repair_result
        change, test_result = repair_result.final_change, repair_result.final_test_result
    if test_result.passed:
        say(f"{test_result.passed_count} Tests bestanden. Ich erstelle den Pull Request.")

    minio = MinioClient(config.minio)
    artifact_key = minio.store_diff(run_id, change.diff)
    minio.save_run_artifact(
        run_id,
        "progress.json",
        json.dumps(tracker.timeline, indent=2).encode("utf-8"),
        "application/json",
    )

    result.code_change = change
    result.test_result = test_result
    result.artifact_key = artifact_key

    if not test_result.passed:  # nothing worth reviewing or shipping
        attempts = result.repair_result.attempts if result.repair_result else 0
        # The diff is rejected, but it is what a human needs to see: it shows what JARVIS tried,
        # and the defect is sometimes in the tests it wrote rather than in the source (JW-28).
        raise ManualInterventionNeeded(
            f"tests still failing after {attempts} repair attempts "
            f"(failed={test_result.failed}, errors={test_result.errors}, exit_code={test_result.exit_code})",
            diff=change.diff,
        )

    machine.transition(RunState.REVIEW)
    try:
        review_result = review(ticket, plan, change, test_result, config)
    except Exception as exc:  # tests passed, so a broken judge must not block the PR
        logger.warning("Judge review failed, continuing without it: %s", exc)
        tracker.note(f"Judge review failed, PR will carry a warning: {exc}")
        review_result = ReviewResult(passed=False, findings=f"⚠️ Judge review failed ({exc}) — bitte manuell reviewen.")
    result.review_result = review_result
    if not review_result.passed:  # V1 feedback data, collection only; best effort, runs in the background
        try:
            record_feedback("judge_warning", ticket_id=ticket.key, run_id=run_id, plan=plan,
                            reason=review_result.findings, minio_settings=config.minio)
        except Exception:
            logger.exception("could not record judge feedback")

    machine.transition(RunState.CREATE_PR)

    def open_pr():
        return create_pr(
            ticket,
            repo_full_name,
            change,
            test_result,
            review_result,
            approver,
            artifact_key,
            config,
            run_id=run_id,
            plan=plan,
            duration_seconds=tracker.elapsed_seconds(),
            state_log=tracker.state_log(),
        )

    try:
        pr_result = open_pr()
    except Exception as exc:  # push and PR creation are idempotent, so one retry is safe
        tracker.note(f"create PR failed ({exc}), retrying once")
        try:
            pr_result = open_pr()
        except Exception as retry_exc:
            raise ManualInterventionNeeded(
                f"PR could not be created after 2 attempts: {retry_exc}", diff=change.diff
            ) from retry_exc
    result.pr_result = pr_result
    say(f"Fertig. Pull Request {_pr_label(pr_result.url)} geöffnet. {test_result.passed_count} Tests bestanden.")

    machine.transition(RunState.NOTIFY)
    repairs = result.repair_result.attempts if result.repair_result else 0
    jira_comment(ticket, pr_result, test_result, review_result, run_id, config, repairs=repairs)
    notify(ticket, pr_result, test_result, review_result, config, repairs=repairs,
           duration_seconds=tracker.elapsed_seconds())


class RunQueue:
    """One run at a time per person; runs of different people run in parallel.

    A run for someone who already has an active run waits here and starts, in order, when that
    run ends. The key is the ticket's Jira assignee ("unassigned" when unknown). In-memory only:
    a restart drops queued runs (the console then shows them as unknown).

    This only keeps one person's runs from stepping on each other. It is NOT a global limit:
    how many parallel runs the LiteLLM / GPU backend can really sustain still needs load
    testing (V1), and there is no total cap or rate limit yet.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}                                   # person -> run_id
        self._waiting: dict[str, deque[tuple[str, Callable[[], None]]]] = {}

    def submit(self, person: str, run_id: str, start: Callable[[], None]) -> int:
        """Start now (returns 0) or queue (returns the 1-based position in this person's queue)."""
        with self._lock:
            if person not in self._active:
                self._active[person] = run_id
                position = 0
            else:
                self._waiting.setdefault(person, deque()).append((run_id, start))
                position = len(self._waiting[person])
        if position == 0:
            self._launch(person, run_id, start)
        return position

    def finished(self, person: str, run_id: str) -> None:
        """Called when a run ends (always, also after errors): start this person's next run."""
        with self._lock:
            if self._active.get(person) != run_id:
                return
            queue = self._waiting.get(person)
            if queue:
                next_id, next_start = queue.popleft()
                self._active[person] = next_id
                if not queue:
                    del self._waiting[person]
            else:
                del self._active[person]
                return
        self._launch(person, next_id, next_start)

    def position(self, run_id: str) -> int:
        with self._lock:
            for queue in self._waiting.values():
                for i, (rid, _) in enumerate(queue, 1):
                    if rid == run_id:
                        return i
        return 0

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"active": len(self._active), "queued": sum(len(q) for q in self._waiting.values())}

    def _launch(self, person: str, run_id: str, start: Callable[[], None]) -> None:
        try:
            start()
        except Exception:  # a start that fails must not block the person's queue forever
            logger.exception("run %s could not be started", run_id)
            self.finished(person, run_id)
