"""JARVIS Console backend (FastAPI).

Run with: uvicorn jarvis.console.server:app --port 8090   (or: python -m jarvis console)

Security model: this API can start code changes, approve plans and create Jira
tickets. It only accepts requests whose Origin is the console page itself (file://
pages send "null", localhost, or the SOKRATES-1 LAN address the service is served on) -
a random website open in the same browser is rejected. There is no login yet
(WMCNL-2533): anyone on the LAN who can open the page can use it.
Approval is never automatic: a run waits until a human presses Approve or Reject.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from jarvis.clients.jira_client import JiraClient
from jarvis.config import ConfigError, JarvisConfig, load_config
from jarvis.models.plan import Plan
from jarvis.models.run import RunResult
from jarvis.pipeline import execute_run
from jarvis.progress import ProgressTracker
from jarvis.state import RunState

logger = logging.getLogger("jarvis.console")

_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML = _ROOT / "console" / "index.html"
_HISTORY_FILE = _ROOT / ".jarvis" / "console_history.json"

# Local dev + the LAN addresses the jarvis-console service on SOKRATES-1 is opened from.
_ALLOWED_ORIGINS = [
    "null", "http://localhost:8090", "http://127.0.0.1:8090", "http://0.0.0.0:8090",
    "http://192.168.178.75:8090", "http://192.168.178.81:8090",
    "https://192.168.178.75:9443",  # HTTPS listener (jarvis.console.serve): browsers allow the mic only here
]
_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_MAX_SPEAK_CHARS = 600
_CHAT_SYSTEM_PROMPT = (
    "Du bist JARVIS, ein KI-Entwicklungsassistent für WAMOCON. Du hilfst Entwicklern mit Code-Fragen, "
    "Jira-Tickets und technischen Problemen. Antworte kurz und präzise auf Deutsch."
)
_CHAT_PRIMARY_MODEL = "jarvis-general"
_CHAT_PRIMARY_TIMEOUT = 25  # seconds; then fall back to the planner model
_CHAT_FALLBACK_TIMEOUT = 90
_CHAT_PRIMARY_COOLDOWN = 300  # after a failure, skip the primary model for 5 minutes
_CHAT_MAX_HISTORY = 20
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_APPROVAL_TIMEOUT_SECONDS = 15 * 60
_HISTORY_LIMIT = 20

app = FastAPI(title="JARVIS Console")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def _local_origin_only(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- run registry


@dataclass
class RunRecord:
    run_id: str
    ticket_id: str
    dry_run: bool
    started_at: str
    status: str = "running"  # running | awaiting_approval | done | cancelled | failed
    state: str = "INIT"
    events: list[dict] = field(default_factory=list)
    plan: dict | None = None
    result: dict | None = None
    _approval: threading.Event = field(default_factory=threading.Event)
    _approved: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_event(self, event: dict) -> None:
        event = {**event, "ts": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.events.append(event)
            if event["type"] == "state":
                self.state = event["state"]

    def snapshot(self, since: int = 0) -> tuple[list[dict], bool]:
        with self._lock:
            return list(self.events[since:]), self.status in ("done", "cancelled", "failed")

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "ticket_id": self.ticket_id,
                "dry_run": self.dry_run,
                "started_at": self.started_at,
                "status": self.status,
                "state": self.state,
                "plan": self.plan,
                "result": self.result,
                "events": list(self.events),
            }


_runs: dict[str, RunRecord] = {}
_runs_lock = threading.Lock()
_history: list[dict] = []
_config: JarvisConfig | None = None


def _get_config() -> JarvisConfig:
    global _config
    if _config is None:
        try:
            _config = load_config(_ROOT / "jarvis.yaml")
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _config


def _load_history() -> None:
    try:
        _history[:] = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _history[:] = []


def _save_history() -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(json.dumps(_history[:_HISTORY_LIMIT], indent=2), encoding="utf-8")
    except OSError:
        logger.warning("could not persist run history")


_load_history()


def _plan_view(plan: Plan) -> dict:
    return {
        "approach": plan.approach,
        "files": [{"path": fc.path, "reason": fc.reason} for fc in plan.files_to_change],
        "test_plan": plan.test_plan,
        "risk_class": plan.risk_class,
        "estimated_tokens": plan.estimated_tokens,
    }


def _result_view(result: RunResult, duration_seconds: float) -> dict:
    tests = result.test_result
    return {
        "state": result.state.value,
        "approved": result.approved,
        "approver": result.approver,
        "error": result.error,
        "duration_seconds": round(duration_seconds, 1),
        "pr_url": result.pr_result.url if result.pr_result else None,
        "branch": result.pr_result.branch if result.pr_result else None,
        "repair_attempts": result.repair_result.attempts if result.repair_result else 0,
        "tests": {
            "passed": tests.passed,
            "exit_code": tests.exit_code,
            "passed_count": tests.passed_count,
            "failed": tests.failed,
            "errors": tests.errors,
            "duration_seconds": round(tests.duration_seconds, 1),
        }
        if tests
        else None,
        "judge": {"passed": result.review_result.passed, "findings": result.review_result.findings}
        if result.review_result
        else None,
    }


def _worker(record: RunRecord, config: JarvisConfig) -> None:
    tracker = ProgressTracker(record.run_id, record.ticket_id)
    tracker.subscribe(record.add_event)

    def approve(plan: Plan, ticket_id: str) -> bool:
        with record._lock:
            record.plan = _plan_view(plan)
            record.status = "awaiting_approval"
        record.add_event({"type": "approval_required", "plan": record.plan})
        decided = record._approval.wait(timeout=_APPROVAL_TIMEOUT_SECONDS)
        if not decided:
            tracker.note("no decision within 15 minutes - treated as rejected")
        with record._lock:
            record.status = "running"
        return decided and record._approved

    started = datetime.now(timezone.utc)
    result = execute_run(
        record.ticket_id,
        config,
        dry_run=record.dry_run,
        approve=approve,
        run_id=record.run_id,
        tracker=tracker,
        # Spoken status lines go to the browser as events; the page plays them via /api/speak.
        narrate=lambda text: record.add_event({"type": "speech", "text": text}),
    )
    duration = (datetime.now(timezone.utc) - started).total_seconds()

    view = _result_view(result, duration)
    status = {RunState.FAILED: "failed", RunState.CANCELLED: "cancelled"}.get(result.state, "done")
    with record._lock:
        record.result = view
        record.state = result.state.value
    record.add_event({"type": "finished", "status": status, "result": view})
    with record._lock:
        record.status = status  # last, so a WebSocket never closes before the "finished" event is queued

    _history.insert(
        0,
        {
            "run_id": record.run_id,
            "ticket_id": record.ticket_id,
            "dry_run": record.dry_run,
            "status": status,
            "started_at": record.started_at,
            "duration_seconds": view["duration_seconds"],
            "pr_url": view["pr_url"],
        },
    )
    del _history[_HISTORY_LIMIT:]
    _save_history()


def _active_run() -> RunRecord | None:
    with _runs_lock:
        for record in _runs.values():
            if record.status in ("running", "awaiting_approval"):
                return record
    return None


# ------------------------------------------------------------------- endpoints


class RunRequest(BaseModel):
    ticket_id: str
    dry_run: bool = True


class ApprovalRequest(BaseModel):
    approved: bool


class TicketRequest(BaseModel):
    project: str
    summary: str = Field(min_length=3, max_length=255)
    description: str = Field(default="", max_length=20000)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=200)
    lang: Literal["de", "en"] = "de"


@app.get("/")
def index() -> FileResponse:
    if not _INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="console/index.html not found")
    return FileResponse(_INDEX_HTML, media_type="text/html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/tickets")
def list_tickets(project: str = "JW") -> list[dict]:
    if not _PROJECT_RE.match(project):
        raise HTTPException(status_code=422, detail="invalid project key")
    tickets = JiraClient(_get_config().jira).search_open(project)
    return [
        {"key": t.key, "summary": t.summary, "status": t.status, "type": t.issue_type, "url": t.url}
        for t in tickets
    ]


@app.post("/api/tickets", status_code=201)
def create_ticket(body: TicketRequest) -> dict:
    if not _PROJECT_RE.match(body.project):
        raise HTTPException(status_code=422, detail="invalid project key")
    ticket = JiraClient(_get_config().jira).create_issue(body.project, body.summary.strip(), body.description)
    return {"key": ticket.key, "summary": ticket.summary, "url": ticket.url}


@app.post("/api/run", status_code=202)
def start_run(body: RunRequest) -> dict:
    if not _TICKET_RE.match(body.ticket_id):
        raise HTTPException(status_code=422, detail="invalid ticket id")
    config = _get_config()
    active = _active_run()
    if active is not None:
        raise HTTPException(status_code=409, detail=f"run {active.ticket_id} is still {active.status}")

    record = RunRecord(
        run_id=str(uuid.uuid4()),
        ticket_id=body.ticket_id,
        dry_run=body.dry_run,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    with _runs_lock:
        _runs[record.run_id] = record
    threading.Thread(target=_worker, args=(record, config), daemon=True, name=f"run-{record.ticket_id}").start()
    return {"run_id": record.run_id}


def _get_run(run_id: str) -> RunRecord:
    record = _runs.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return record


@app.get("/api/run/{run_id}/status")
def run_status(run_id: str) -> dict:
    record = _runs.get(run_id)
    if record is not None:
        return record.view()
    for entry in _history:  # runs from before a server restart: summary only
        if entry["run_id"] == run_id:
            return {**entry, "events": [], "plan": None, "result": None, "state": entry["status"].upper()}
    raise HTTPException(status_code=404, detail="unknown run")


@app.post("/api/run/{run_id}/approval")
def decide(run_id: str, body: ApprovalRequest) -> dict:
    record = _get_run(run_id)
    with record._lock:
        if record.status != "awaiting_approval":
            raise HTTPException(status_code=409, detail="run is not waiting for approval")
        record._approved = body.approved
    record._approval.set()
    return {"approved": body.approved}


@app.post("/api/voice-input")
async def voice_input(request: Request) -> dict:
    """Raw audio body (webm/ogg/wav from the browser's MediaRecorder) -> Whisper -> ticket key."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=422, detail="empty audio")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    content_type = request.headers.get("content-type", "audio/webm").split(";")[0].strip()
    extension = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/mp4": "m4a"}.get(content_type, "webm")

    # Imported here: jarvis.steps.voice_input pulls in sounddevice, which the console does not need otherwise.
    from jarvis.steps.voice_input import extract_ticket_id, parse_yes_no, transcribe_audio

    try:
        transcript = await asyncio.to_thread(
            transcribe_audio, audio, _get_config(), filename=f"audio.{extension}", content_type=content_type
        )
    except Exception as exc:
        logger.warning("voice transcription failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Whisper not reachable: {type(exc).__name__}") from exc
    ticket_id = extract_ticket_id(transcript)
    # answer: True = ja, False = nein, None = unclear or silence (never treated as approval).
    return {
        "success": ticket_id is not None,
        "ticket_id": ticket_id,
        "transcript": transcript,
        "answer": parse_yes_no(transcript),
    }


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=_MAX_SPEAK_CHARS)


@app.post("/api/speak")
async def speak(body: SpeakRequest) -> Response:
    """Text -> openedai-speech on CAESAR -> WAV for the browser to play."""
    url = f"{_get_config().voice.tts_url.rstrip('/')}/v1/audio/speech"
    payload = {"model": "tts-1", "input": body.text, "voice": "alloy", "response_format": "wav"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("speech synthesis failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Speech service not reachable: {type(exc).__name__}") from exc
    return Response(content=response.content, media_type="audio/wav", headers={"Cache-Control": "no-store"})


_chat_primary_down_until = 0.0


@app.post("/api/chat")
def chat(body: ChatRequest) -> dict:
    """One chat turn with JARVIS. History lives in the browser session only; nothing is stored here."""
    global _chat_primary_down_until
    from openai import OpenAI

    system = _CHAT_SYSTEM_PROMPT + ("" if body.lang == "de" else " The user reads English: answer in English.")
    messages = [{"role": "system", "content": system}]
    messages += [turn.model_dump() for turn in body.history[-_CHAT_MAX_HISTORY:]]
    messages.append({"role": "user", "content": body.message})

    config = _get_config()
    candidates = [(config.models.planner, _CHAT_FALLBACK_TIMEOUT)]
    if time.monotonic() >= _chat_primary_down_until:
        candidates.insert(0, (_CHAT_PRIMARY_MODEL, _CHAT_PRIMARY_TIMEOUT))
    last_error: Exception | None = None
    for model, timeout in candidates:
        client = OpenAI(base_url=config.litellm.base_url, api_key=config.litellm.api_key, timeout=timeout, max_retries=0)
        try:
            reply = client.chat.completions.create(model=model, messages=messages, temperature=0.3, max_tokens=800)
            return {"response": (reply.choices[0].message.content or "").strip(), "model": model}
        except Exception as exc:
            last_error = exc
            logger.warning("chat model %s failed: %s", model, type(exc).__name__)
            if model == _CHAT_PRIMARY_MODEL:
                _chat_primary_down_until = time.monotonic() + _CHAT_PRIMARY_COOLDOWN
    raise HTTPException(status_code=502, detail=f"LiteLLM not reachable: {type(last_error).__name__}")


@app.get("/api/runs")
def recent_runs(limit: int = 5) -> list[dict]:
    return _history[: max(1, min(limit, _HISTORY_LIMIT))]


@app.websocket("/ws/{run_id}")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    record = _runs.get(run_id)
    if record is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    sent = 0
    try:
        while True:
            events, finished = record.snapshot(since=sent)
            for event in events:
                await websocket.send_json(event)
            sent += len(events)
            if finished and not events:
                break
            await asyncio.sleep(0.25)
        await websocket.close()
    except WebSocketDisconnect:
        return
