"""RunState dataclass + state machine for a single JARVIS run."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.progress import ProgressTracker

logger = logging.getLogger("jarvis.state")


class RunState(str, Enum):
    INIT = "INIT"
    READ_TICKET = "READ_TICKET"
    FIND_REPO = "FIND_REPO"
    READ_REPO = "READ_REPO"
    PLAN = "PLAN"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"
    CODE_CHANGE = "CODE_CHANGE"
    RUN_TESTS = "RUN_TESTS"
    REPAIR = "REPAIR"
    REVIEW = "REVIEW"
    CREATE_PR = "CREATE_PR"
    NOTIFY = "NOTIFY"
    DONE = "DONE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# Spoken / demo names of each step.
STATE_LABELS_DE: dict[RunState, str] = {
    RunState.INIT: "Start",
    RunState.READ_TICKET: "Ticket lesen",
    RunState.FIND_REPO: "Repository finden",
    RunState.READ_REPO: "Repository laden",
    RunState.PLAN: "Plan erstellen",
    RunState.AWAIT_APPROVAL: "Freigabe",
    RunState.CODE_CHANGE: "Code ändern",
    RunState.RUN_TESTS: "Tests ausführen",
    RunState.REPAIR: "Reparatur",
    RunState.REVIEW: "Code-Review",
    RunState.CREATE_PR: "Pull Request erstellen",
    RunState.NOTIFY: "Benachrichtigen",
    RunState.DONE: "Fertig",
    RunState.CANCELLED: "Abgebrochen",
    RunState.FAILED: "Fehlgeschlagen",
}

# Allowed forward transitions through Step E + F (REVIEW -> CREATE_PR -> NOTIFY -> DONE).
# REPAIR is entered either when CODE_CHANGE produced no usable diff (then tests run
# after it) or when tests fail. If the repair loop cannot fix it, the run goes to
# FAILED via StateMachine.fail() - never DONE - and a human is asked to step in.
_TRANSITIONS: dict[RunState, tuple[RunState, ...]] = {
    RunState.INIT: (RunState.READ_TICKET,),
    RunState.READ_TICKET: (RunState.FIND_REPO,),
    RunState.FIND_REPO: (RunState.READ_REPO,),
    RunState.READ_REPO: (RunState.PLAN,),
    RunState.PLAN: (RunState.AWAIT_APPROVAL,),
    RunState.AWAIT_APPROVAL: (RunState.CODE_CHANGE, RunState.DONE, RunState.CANCELLED),
    RunState.CODE_CHANGE: (RunState.RUN_TESTS, RunState.REPAIR),
    RunState.RUN_TESTS: (RunState.REPAIR, RunState.REVIEW),
    RunState.REPAIR: (RunState.RUN_TESTS, RunState.REVIEW),
    RunState.REVIEW: (RunState.CREATE_PR,),
    RunState.CREATE_PR: (RunState.NOTIFY,),
    RunState.NOTIFY: (RunState.DONE,),
}


@dataclass
class StateMachine:
    run_id: str
    state: RunState = RunState.INIT
    history: list[tuple[str, RunState]] = field(default_factory=list)
    tracker: "ProgressTracker | None" = None

    def __post_init__(self) -> None:
        self._log_transition(self.state)

    def _track(self, state: RunState) -> None:
        if self.tracker is not None:
            self.tracker.on_transition(state)

    def transition(self, next_state: RunState) -> None:
        allowed = _TRANSITIONS.get(self.state, ())
        if next_state not in allowed:
            raise ValueError(f"Illegal transition {self.state.value} -> {next_state.value}")
        self.state = next_state
        self._log_transition(next_state)
        self._track(next_state)

    def fail(self, error: Exception) -> None:
        self.state = RunState.FAILED
        self._log_transition(RunState.FAILED, extra=str(error))
        self._track(RunState.FAILED)

    def _log_transition(self, state: RunState, extra: str = "") -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        self.history.append((timestamp, state))
        message = f"[{timestamp}] run={self.run_id} state={state.value}"
        if extra:
            message += f" error={extra}"
        logger.info(message)
