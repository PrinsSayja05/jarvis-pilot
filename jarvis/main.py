"""CLI entry point: python -m jarvis WM-431 --dry-run   |   python -m jarvis console"""
from __future__ import annotations

import getpass
import logging
import time
import uuid
from typing import Optional

import typer

from jarvis import audit
from jarvis.clients.github_client import log_auth_mode
from jarvis.config import ConfigError, JarvisConfig, load_config
from jarvis.models.run import RunResult
from jarvis.progress import ProgressTracker, print_banner
from jarvis.steps.read_repo import cleanup_stale_clones
from jarvis.pipeline import execute_run
from jarvis.state import RunState
from jarvis.steps.approval import print_plan, request_approval
from jarvis.steps.voice_input import listen_yes_no, voice_input
from jarvis.steps.voice_output import speak, speak_async, wait_until_spoken

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("jarvis.main")

app = typer.Typer(add_completion=False)

_VOICE_APPROVAL_TRIES = 2  # ask once more when the answer was unclear


def _voice_approve(config: JarvisConfig):
    """Approval by voice: 'Ja' / 'Nein'. Falls back to the typed prompt; never approves on silence."""

    def approve(plan, ticket_id: str) -> bool:
        print_plan(plan, ticket_id)
        if plan.risk_class == "high":  # a spoken "ja" is too easy for a high risk change
            speak("Hohes Risiko. Bitte bestätigen Sie im Terminal.", config)
            return request_approval(plan)[0]
        for attempt in range(1, _VOICE_APPROVAL_TRIES + 1):
            speak("Soll ich fortfahren? Sagen Sie Ja oder Nein.", config)
            try:
                answer = listen_yes_no(config)
            except Exception as exc:
                print(f"Spracheingabe nicht verfügbar ({type(exc).__name__}: {exc}) - bitte tippen.")
                speak("Spracheingabe nicht verfügbar. Bitte bestätigen Sie im Terminal.", config)
                return request_approval(plan)[0]
            if answer is not None:
                return answer
            if attempt < _VOICE_APPROVAL_TRIES:
                speak("Ich habe Sie nicht verstanden.", config)
        speak("Keine klare Antwort. Bitte bestätigen Sie im Terminal.", config)
        return request_approval(plan)[0]

    return approve


def _audited(approve, run_id: str, dry_run: bool, config: JarvisConfig, *, source: str):
    """Same decision as `approve`; afterwards it is written to the approval log (and a rejection to feedback)."""
    def wrapped(plan, ticket_id: str) -> bool:
        approved = approve(plan, ticket_id)
        try:
            audit.record_approval(run_id=run_id, ticket_id=ticket_id, decision="approved" if approved else "rejected",
                                  actor=getpass.getuser(), actor_source=source, risk_class=plan.risk_class, dry_run=dry_run)
            if not approved and source != "auto-approve":  # a rule refusing a high-risk plan is not human feedback
                audit.record_feedback("plan_rejected", ticket_id=ticket_id, run_id=run_id, plan=plan,
                                      reason="(im Terminal abgelehnt, kein Grund erfasst)", minio_settings=config.minio,
                                      extra={"actor": getpass.getuser(), "actor_source": source, "dry_run": dry_run})
        except Exception:
            logger.exception("could not write the approval audit")
        return approved
    return wrapped


def _log_run(result: RunResult, dry_run: bool, duration: float) -> None:
    status = {RunState.FAILED: "failed", RunState.CANCELLED: "cancelled"}.get(result.state, "done")
    audit.record_run({
        "run_id": result.run_id, "ticket_id": result.ticket_id, "dry_run": dry_run, "status": status,
        "started_at": result.started_at, "duration_seconds": round(duration, 1), "source": "cli",
        "pr_url": result.pr_result.url if result.pr_result else None, "approved": result.approved,
        "risk_class": result.plan.risk_class if result.plan else None,
        "repair_attempts": (result.repair_result.attempts if result.repair_result else 0) if not dry_run else None,
        "judge_warning": (not result.review_result.passed) if result.review_result else None,
    })


def _cli_approve(plan, ticket_id: str) -> bool:
    print_plan(plan, ticket_id)
    approved, _response = request_approval(plan)
    return approved


def _auto_approve(plan, ticket_id: str) -> bool:
    """--auto-approve (testing only): approve without a prompt, but never a high-risk plan."""
    print_plan(plan, ticket_id)
    if plan.risk_class == "high":
        logger.warning("--auto-approve refuses HIGH risk plans: rejected, run it without --auto-approve")
        return False
    logger.warning("--auto-approve: plan approved automatically (TESTING ONLY)")
    return True


def start_console() -> None:
    """Serve the web console on http://localhost:8090 (auto-reload for development)."""
    import uvicorn

    uvicorn.run("jarvis.console.server:app", host="127.0.0.1", port=8090, reload=True)


@app.command()
def run(
    ticket_id: Optional[str] = typer.Argument(
        None,
        help='Jira ticket key, e.g. WM-431. Omit when using --voice. Use "console" to start the web console.',
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Read ticket, plan, print plan, ask for approval - no code change.",
    ),
    voice: bool = typer.Option(
        False,
        "--voice",
        help="Voice mode: say the ticket (if not given), hear every status, approve with 'Ja' / 'Nein'.",
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="For an audience: big step banners, spoken steps and status, 2s pause between steps.",
    ),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        help="TESTING ONLY: approve low/medium risk plans without asking. High risk plans are still rejected.",
    ),
) -> Optional[RunResult]:
    # "console" is a keyword rather than a subcommand so `python -m jarvis WMCNL-1 --dry-run` keeps working.
    if ticket_id == "console":
        start_console()
        return None
    if not voice and not ticket_id:
        raise typer.BadParameter("ticket_id is required unless --voice is used.")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        raise typer.Exit(code=1) from exc
    log_auth_mode(config.github)
    cleanup_stale_clones()

    talk = voice or demo
    narrate = (lambda text: speak_async(text, config)) if talk else None

    if demo:
        print_banner("JARVIS DEMO MODE")
        speak_async("JARVIS Demo gestartet.", config)

    if voice and not ticket_id:
        speak("Welches Ticket soll ich bearbeiten?", config)
        try:
            ticket_id = voice_input(config)
        except Exception as exc:
            logger.error("Voice input failed (%s: %s) - is faster-whisper up at %s?",
                         type(exc).__name__, exc, config.voice.stt_url)
            raise typer.Exit(code=1) from exc
        if not ticket_id:
            print("Could not understand ticket ID. Please try again.")
            speak("Ich habe keine Ticketnummer verstanden. Bitte versuchen Sie es noch einmal.", config)
            raise typer.Exit(code=1)

    if auto_approve:
        approve = _auto_approve
    elif voice:
        approve = _voice_approve(config)
    else:
        approve = _cli_approve

    run_id = str(uuid.uuid4())
    approve = _audited(approve, run_id, dry_run, config, source="auto-approve" if auto_approve else "cli")
    tracker = ProgressTracker(run_id, ticket_id, demo=demo, speak=narrate)
    started = time.monotonic()
    result = execute_run(
        ticket_id,
        config,
        dry_run=dry_run,
        approve=approve,
        run_id=run_id,
        tracker=tracker,
        auto_approved=auto_approve,
        narrate=narrate,
    )

    _log_run(result, dry_run, time.monotonic() - started)

    if demo:
        print_banner("DEMO COMPLETE", style="bold green")
        speak_async("JARVIS Demo abgeschlossen.", config)
    if talk:
        wait_until_spoken()  # the voice thread is a daemon: let it finish before exiting
    if result.state == RunState.FAILED:
        raise typer.Exit(code=1)
    return result


if __name__ == "__main__":
    app()
