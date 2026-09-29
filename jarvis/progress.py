"""ProgressTracker - reports every state transition of a run with timings."""
from __future__ import annotations

import time
from typing import Callable

from rich.console import Console

from jarvis.state import STATE_LABELS_DE, RunState

_console = Console()
_DEMO_PAUSE_SECONDS = 2
_DEMO_LINE_DELAY = 0.04  # "typing" speed of the ASCII-art banners


def print_banner(text: str, style: str = "bold cyan") -> None:
    """Big ASCII-art text, typed out line by line (plain rule if pyfiglet is missing)."""
    try:
        import pyfiglet

        art = pyfiglet.figlet_format(text, font="standard", width=_console.width)
    except ImportError:
        _console.rule(f"[{style}]{text}[/{style}]", style="cyan")
        return
    for line in art.rstrip("\n").splitlines():
        _console.print(line, style=style, highlight=False, markup=False)
        time.sleep(_DEMO_LINE_DELAY)


class ProgressTracker:
    def __init__(
        self,
        run_id: str,
        ticket_id: str = "",
        demo: bool = False,
        speak: Callable[[str], None] | None = None,
    ) -> None:
        self._demo = demo
        self._speak = speak  # demo mode: say each step name
        self._listeners: list[Callable[[dict], None]] = []
        self._run_id = run_id
        self._ticket_id = ticket_id
        self._started = time.monotonic()
        self._last = self._started
        self.timeline: list[dict] = []

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    def state_log(self) -> list[str]:
        return [f"{e['state']} (+{e['elapsed_seconds']:.1f}s)" for e in self.timeline]

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        """Receive {"type": "state"|"note", ...} events, e.g. for the web console."""
        self._listeners.append(listener)

    def _emit(self, event: dict) -> None:
        for listener in self._listeners:
            listener(event)

    def set_ticket(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

    def on_transition(self, state: RunState) -> None:
        now = time.monotonic()
        previous = self.timeline[-1]["state"] if self.timeline else None
        self.timeline.append(
            {
                "state": state.value,
                "elapsed_seconds": round(now - self._started, 2),
                "previous_state": previous,
                "previous_state_seconds": round(now - self._last, 2),
            }
        )
        self._last = now
        self._emit({"type": "state", "state": state.value, "elapsed_seconds": round(now - self._started, 1)})
        if self._demo:
            print_banner(state.value.replace("_", " "))
            if self._speak is not None:
                self._speak(f"Schritt: {STATE_LABELS_DE.get(state, state.value)}")
            if state not in (RunState.DONE, RunState.FAILED, RunState.CANCELLED):
                time.sleep(_DEMO_PAUSE_SECONDS)
        _console.print(
            f"[dim][+{now - self._started:6.1f}s][/dim] {self._ticket_id} -> [bold]{state.value}[/bold]"
        )

    def note(self, message: str) -> None:
        self._emit({"type": "note", "message": message, "elapsed_seconds": round(self.elapsed_seconds(), 1)})
        _console.print(f"[dim][+{time.monotonic() - self._started:6.1f}s][/dim]   {message}")
