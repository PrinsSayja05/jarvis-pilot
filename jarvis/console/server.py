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
import os
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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from jarvis.clients.jira_client import JiraClient
from jarvis.console.auth import auth_mode, current_user
from jarvis.console.auth import router as auth_router
from jarvis.config import ConfigError, JarvisConfig, load_config
from jarvis.models.plan import Plan
from jarvis.models.run import RunResult
from jarvis.models.ticket import priority_rank
from jarvis import audit
from jarvis.pipeline import RunQueue, execute_run
from jarvis.progress import ProgressTracker
from jarvis.state import RunState

logger = logging.getLogger("jarvis.console")

_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML = _ROOT / "console" / "index.html"
_HISTORY_FILE = _ROOT / ".jarvis" / "console_history.json"
_CEO_HTML = _ROOT / "console" / "ceo.html"
# Jira account ids that see the CEO view link. Editable without touching .env or restarting.
_ADMINS_FILE = _ROOT / ".jarvis" / "admins.json"

# Local dev + the LAN addresses the jarvis-console service on SOKRATES-1 is opened from.
# CONSOLE_EXTRA_ORIGINS (comma separated) adds more, e.g. a test instance on another port.
_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CONSOLE_EXTRA_ORIGINS", "").split(",") if o.strip()] + [
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
_WS_LINGER_SECONDS = 20  # how long a finished run's socket waits for the self-rating note
_HISTORY_LIMIT = 20

app = FastAPI(title="JARVIS Console")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# Reachable without a login even when Keycloak is active.
_PUBLIC_PATHS = ("/auth/", "/api/health", "/api/whoami")


@app.middleware("http")
async def _require_login(request: Request, call_next):
    """Keycloak active: no session -> the page redirects to the login, the API answers 401.
    Keycloak not configured or unreachable: demo mode, nothing is locked (see jarvis.console.auth)."""
    path = request.url.path
    if not path.startswith(_PUBLIC_PATHS):
        mode, _ = await asyncio.to_thread(auth_mode)
        if mode == "keycloak" and current_user(request) is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse(f"/auth/login?next={path}")
    return await call_next(request)


@app.middleware("http")
async def _local_origin_only(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    return await call_next(request)


app.include_router(auth_router)


# ---------------------------------------------------------------- run registry


@dataclass
class RunRecord:
    run_id: str
    ticket_id: str
    dry_run: bool
    started_at: str
    status: str = "running"  # queued | running | awaiting_approval | done | cancelled | failed
    person: str = "unassigned"  # ticket assignee: the run queue key
    assignee_name: str = ""     # shown when someone else presses the Telegram button
    actor: str = ""             # who started it (demo-selected name, not verified)
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
                "queue_position": _queue.position(self.run_id) if self.status == "queued" else 0,
                "plan": self.plan,
                "result": self.result,
                "events": list(self.events),
            }


_runs: dict[str, RunRecord] = {}
_runs_lock = threading.Lock()
_queue = RunQueue()  # one run at a time per assignee, different assignees in parallel
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


@app.on_event("startup")
def _log_auth_mode() -> None:
    mode, reason = auth_mode()
    logger.info("startup: console auth mode = %s (%s)", mode, reason)


@app.on_event("startup")
def _start_telegram_bot() -> None:
    """Approvals from Telegram. Polling, because SOKRATES-1 is not reachable from the internet."""
    try:
        from jarvis.console import telegram_bot

        started = telegram_bot.start(_get_config(), lambda rid: _runs.get(rid), apply_decision)
        if started:
            logger.info("startup: telegram approvals active (%d user(s) mapped in %s)",
                        len(telegram_bot.load_user_map()), telegram_bot.USER_MAP_FILE)
    except Exception as exc:
        logger.warning("startup: telegram approvals not started (%s)", exc)


@app.on_event("startup")
def _cleanup_stale_clones() -> None:
    from jarvis.steps.read_repo import cleanup_stale_clones

    logger.info("startup: removed %d stale clone dir(s)", cleanup_stale_clones())


@app.on_event("startup")
def _log_github_auth() -> None:
    try:
        from jarvis.clients.github_client import log_auth_mode

        log_auth_mode(_get_config().github)
    except Exception as exc:  # a config problem shows up on the first request anyway
        logger.warning("GitHub auth: not determined at startup (%s)", exc)


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
            audit.record_approval(run_id=record.run_id, ticket_id=record.ticket_id, decision="timeout", actor="system",
                                  actor_source="system", risk_class=plan.risk_class, dry_run=record.dry_run,
                                  reason="keine Entscheidung innerhalb von 15 Minuten")
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

    entry = {
        "run_id": record.run_id,
        "ticket_id": record.ticket_id,
        "dry_run": record.dry_run,
        "status": status,
        "started_at": record.started_at,
        "duration_seconds": view["duration_seconds"],
        "pr_url": view["pr_url"],
    }
    _history.insert(0, entry)
    del _history[_HISTORY_LIMIT:]
    _save_history()
    audit.record_run({**entry, "source": "console", "person": record.person, "approved": view["approved"],
                      "risk_class": (record.plan or {}).get("risk_class"),
                      "repair_attempts": view["repair_attempts"] if not record.dry_run else None,
                      "judge_warning": (not view["judge"]["passed"]) if view["judge"] else None})


def _run_and_release(record: RunRecord, config: JarvisConfig) -> None:
    try:
        _worker(record, config)
    finally:  # whatever happened, the person's next queued run may start
        _queue.finished(record.person, record.run_id)


def _person_of(ticket_id: str) -> tuple[str, str]:
    """(accountId, display name) of the ticket's assignee. The id is the run queue key and decides
    who may press the Telegram button; unknown -> "unassigned", which queues conservatively."""
    try:
        ticket = JiraClient(_get_config().jira).get_ticket(ticket_id)
        _assignee_cache[ticket_id] = (time.monotonic(), ticket.assignee_id)
        return ticket.assignee_id or "unassigned", ticket.assignee_name
    except Exception as exc:
        logger.warning("assignee lookup for the run queue failed: %s", type(exc).__name__)
        return "unassigned", ""


def _actor(request: Request, claimed: str) -> tuple[str, str]:
    """(name, source). Keycloak: the logged-in person. Demo mode: the name picked in the dropdown,
    sent by the page and NOT verified; the source says so."""
    mode, _ = auth_mode()
    user = current_user(request) if mode == "keycloak" else None
    if user:
        return user.get("name") or user.get("username") or "?", "keycloak"
    return (claimed.strip() or "Alle (keine Person gewählt)"), "demo-auswahl"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


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
    actor: str = Field(default="", max_length=100)   # demo-selected name, for the access log only


class ApprovalRequest(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=500)  # optional, for a rejection
    actor: str = Field(default="", max_length=100)


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


@app.get("/api/config")
def public_config() -> dict:
    """Non-secret settings the page needs (e.g. the GitHub link). Never tokens or keys."""
    github = _get_config().github
    return {"github_org": github.org, "pilot_repo": github.pilot_repo, "github_auth": github.auth_mode}


_RATING_WINDOW = 10


def _rating_trend(ordered: list[dict]) -> dict:
    """Average of the last 10 rated runs against the 10 before them. Runs that were never rated
    are skipped, so the comparison is always ten scores against ten scores."""
    scores = [r["self_rating"]["score"] for r in ordered
              if isinstance(r.get("self_rating"), dict) and isinstance(r["self_rating"].get("score"), int)]
    recent, earlier = scores[:_RATING_WINDOW], scores[_RATING_WINDOW:2 * _RATING_WINDOW]
    average = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    current, previous = average(recent), average(earlier)
    if current is None or previous is None:
        trend = "unbekannt"
    elif current - previous >= 0.3:
        trend = "verbessert sich"
    elif previous - current >= 0.3:
        trend = "wird schlechter"
    else:
        trend = "stabil"
    return {
        "current": current,
        "previous": previous,
        "trend": trend,
        "rated_runs": len(scores),
        "window": _RATING_WINDOW,
        "series": list(reversed(recent)),   # oldest first, for a left to right sparkline
    }


def _fill_assignee_names(ticket_ids: list[str]) -> None:
    """One Jira lookup for every ticket in the table whose assignee we have not seen yet.
    Best effort: a name is decoration, its absence must not break the overview."""
    unknown = sorted({t for t in ticket_ids if t and _TICKET_RE.match(t) and t not in _assignee_name_cache})
    if not unknown:
        return
    try:
        client = JiraClient(_get_config().jira)
        by_account = {p["account_id"]: p["name"] for p in _demo_people()}
        for key, account in client.assignees_of(unknown).items():
            _assignee_name_cache[key] = by_account.get(account, "")
    except Exception as exc:
        logger.info("assignee names for the CEO view failed: %s", type(exc).__name__)
    for key in unknown:                     # do not ask again for tickets that have no assignee
        _assignee_name_cache.setdefault(key, "")


def _admin_accounts() -> list[str]:
    """Who is offered the CEO view. This decides what the console *shows*, not what it allows:
    /api/ceo-dashboard and /ceo answer anyone on the network, exactly like the rest of the console
    today. Real access control arrives with the Keycloak login (WMCNL-2533, WMCNL-2606)."""
    try:
        data = json.loads(_ADMINS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("admins", [])
    return [str(a) for a in data if isinstance(a, str)] if isinstance(data, list) else []


@app.get("/ceo")
def ceo_page() -> FileResponse:
    if not _CEO_HTML.is_file():
        raise HTTPException(status_code=404, detail="console/ceo.html not found")
    return FileResponse(_CEO_HTML, media_type="text/html")


_STATUS_DE = {"done": "fertig", "failed": "fehlgeschlagen", "cancelled": "abgebrochen",
              "running": "läuft", "awaiting_approval": "wartet auf Freigabe", "queued": "wartet"}
_SOURCE_DE = {"telegram": "Telegram", "keycloak": "Konsole (angemeldet)",
              "demo-auswahl": "Konsole (Demo-Auswahl)", "cli": "Terminal",
              "auto-approve": "Terminal (auto)", "system": "Zeitablauf"}


@app.get("/api/ceo-dashboard")
def ceo_dashboard(limit: int = 20) -> dict:
    """Read-only overview for management. No approve or reject here by design.

    Security: open to anyone who can reach the console, like every other endpoint while the
    Keycloak login is deferred. It exposes ticket keys, names and PR links, no secrets. Before
    this is used outside the office network it needs the real login (WMCNL-2533, WMCNL-2606).
    """
    limit = max(1, min(limit, 100))
    runs = {r["run_id"]: r for r in audit.merged_runs()}

    # who decided each run, and from where
    decisions: dict[str, dict] = {}
    for row in audit.read_jsonl(audit.APPROVAL_LOG):
        decisions.setdefault(row.get("run_id", ""), row)

    live = {r.run_id: r for r in list(_runs.values())}
    for run_id, record in live.items():                     # runs still going, not yet in the log
        runs.setdefault(run_id, {"run_id": run_id, "ticket_id": record.ticket_id,
                                 "status": record.status, "started_at": record.started_at,
                                 "dry_run": record.dry_run})

    ordered = sorted(runs.values(), key=lambda r: r.get("started_at") or r.get("logged_at") or "",
                     reverse=True)
    today = datetime.now(timezone.utc).date().isoformat()
    todays = [r for r in ordered if (r.get("started_at") or "").startswith(today)]
    finished_today = [r for r in todays if r.get("status") in ("done", "failed", "cancelled")]
    done_today = [r for r in finished_today if r.get("status") == "done"]
    durations = [r["duration_seconds"] for r in todays if isinstance(r.get("duration_seconds"), (int, float))]

    _fill_assignee_names([r.get("ticket_id", "") for r in ordered[:limit]])

    rows = []
    for r in ordered[:limit]:
        record = live.get(r["run_id"])
        decision = decisions.get(r["run_id"], {})
        assignee = ""
        if record is not None:
            assignee = record.assignee_name
        if not assignee:
            assignee = _assignee_name_cache.get(r.get("ticket_id", ""), "")
        rows.append({
            "run_id": r["run_id"],
            "ticket_id": r.get("ticket_id", ""),
            "assignee": assignee,
            "status": r.get("status", ""),
            "status_de": _STATUS_DE.get(r.get("status", ""), r.get("status", "")),
            "dry_run": bool(r.get("dry_run")),
            "started_at": r.get("started_at"),
            "duration_seconds": r.get("duration_seconds"),
            "approved_by": decision.get("actor", ""),
            "approved_via": _SOURCE_DE.get(decision.get("actor_source", ""), decision.get("actor_source", "")),
            "decision": decision.get("decision", ""),
            "pr_url": r.get("pr_url"),
            "self_rating": (r.get("self_rating") or {}).get("score"),
            "self_reasoning": (r.get("self_rating") or {}).get("reasoning", ""),
        })

    config = _get_config()
    # Tickets route to different repos via their repo: label, so counting only the pilot repo
    # would miss PRs. Look at the pilot repo plus every repo a recent run opened a PR in.
    repos = {f"{config.github.org}/{config.github.pilot_repo}"}
    for r in ordered[:50]:
        match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/", r.get("pr_url") or "")
        if match:
            repos.add(match.group(1))
    open_prs = []
    for repo in sorted(repos):
        try:
            open_prs += [url for branch, url in _open_prs(repo).items()
                         if branch.startswith(config.git.branch_prefix)]
        except Exception as exc:
            logger.info("open PR count for %s failed: %s", repo, type(exc).__name__)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_today": len(todays),
        "finished_today": len(finished_today),
        "success_rate": round(100 * len(done_today) / len(finished_today)) if finished_today else None,
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "self_rating": _rating_trend(ordered),
        "open_prs": len(open_prs),
        "open_pr_urls": open_prs[:20],
        "active_runs": _queue.counts(),
        "rows": rows,
    }


@app.get("/api/health")
def health() -> dict:
    # "runs": system load for the console header (active runs, runs waiting in a per-person queue)
    return {"status": "ok", "runs": _queue.counts()}


_PR_CACHE_SECONDS = 60
_open_pr_cache: dict[str, tuple[float, dict[str, str]]] = {}   # repo -> (fetched at, {branch: PR url})


def _open_prs(repo: str) -> dict[str, str]:
    """Open PRs of one repo as {head branch: url}. Cached briefly; GitHub trouble just means no badge."""
    hit = _open_pr_cache.get(repo)
    if hit and time.monotonic() - hit[0] < _PR_CACHE_SECONDS:
        return hit[1]
    from jarvis.clients.github_client import GitHubClient

    prs: dict[str, str] = {}
    try:
        for pr in GitHubClient(_get_config().github).get_repo(repo).get_pulls(state="open"):
            prs[pr.head.ref] = pr.html_url
    except Exception as exc:
        logger.info("open PR lookup for %s failed: %s", repo, type(exc).__name__)
        return hit[1] if hit else {}
    _open_pr_cache[repo] = (time.monotonic(), prs)
    return prs


def _open_pr_for(ticket, config: JarvisConfig) -> str | None:
    """The open JARVIS PR of a ticket (branch jarvis/<key>), in the repo its repo: label points to."""
    from jarvis.steps.find_repo import choose_repo

    try:
        repo, _reason = choose_repo(ticket, config)
    except ValueError:
        return None
    return _open_prs(repo).get(f"{config.git.branch_prefix}{ticket.key.lower()}")


_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:\-]{6,128}$")
_account_cache: dict[str, tuple[float, tuple[str, str] | None]] = {}
_people_cache: dict[str, tuple[float, list[dict]]] = {}
_assignee_cache: dict[str, tuple[float, str]] = {}
_assignee_name_cache: dict[str, str] = {}   # ticket key -> display name, filled as tickets are listed


def _jira_account(email: str) -> tuple[str, str] | None:
    """Keycloak e-mail -> (Jira accountId, name), cached for 10 minutes."""
    hit = _account_cache.get(email.lower())
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        found = JiraClient(_get_config().jira).find_account_by_email(email) if email else None
    except Exception as exc:
        logger.warning("jira account lookup for %s failed: %s", email, type(exc).__name__)
        return hit[1] if hit else None
    _account_cache[email.lower()] = (time.monotonic(), found)
    return found


def _demo_people(project: str = "JW") -> list[dict]:
    """The people the demo switcher offers: everyone who has open tickets in the project."""
    hit = _people_cache.get(project)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    people: dict[str, str] = {}
    for t in JiraClient(_get_config().jira).search_open(project, limit=100):
        if t.assignee_id:
            people[t.assignee_id] = t.assignee_name
    result = sorted(({"account_id": a, "name": n} for a, n in people.items()), key=lambda p: p["name"].lower())
    _people_cache[project] = (time.monotonic(), result)
    return result


def _view_as(request: Request, requested: str) -> str | None:
    """Whose tickets and history to show. None = everybody (demo mode, "Alle").
    Keycloak: the logged-in person; only an admin may pick someone else (demo switcher)."""
    requested = requested.strip()
    if requested and requested != "all" and not _ACCOUNT_RE.match(requested):
        raise HTTPException(status_code=422, detail="invalid assignee")
    mode, _ = auth_mode()
    if mode != "keycloak":
        return None if requested in ("", "all") else requested
    user = current_user(request) or {}
    if user.get("admin") and requested:
        return None if requested == "all" else requested
    account = _jira_account(user.get("email", ""))
    return account[0] if account else "-"          # "-": logged in, but no Jira account -> nothing


@app.get("/api/whoami")
def whoami(request: Request) -> dict:
    mode, reason = auth_mode()
    user = current_user(request) if mode == "keycloak" else None
    body: dict[str, Any] = {"mode": mode, "reason": reason, "user": None, "demo_people": []}
    if user:
        account = _jira_account(user.get("email", ""))
        body["user"] = {"name": user.get("name"), "username": user.get("username"), "email": user.get("email"),
                        "admin": bool(user.get("admin")), "jira_account_id": account[0] if account else None,
                        "jira_name": account[1] if account else None}
    body["admins"] = _admin_accounts()   # which accounts are offered the CEO view
    if mode != "keycloak" or (user and user.get("admin")):
        try:
            body["demo_people"] = _demo_people()
        except Exception as exc:
            logger.warning("demo people lookup failed: %s", type(exc).__name__)
    return body


@app.get("/api/tickets")
def list_tickets(request: Request, project: str = "JW", assignee: str = "") -> list[dict]:
    if not _PROJECT_RE.match(project):
        raise HTTPException(status_code=422, detail="invalid project key")
    config = _get_config()
    view = _view_as(request, assignee)
    if view == "-":
        return []
    tickets = sorted(JiraClient(config.jira).search_open(project, assignee_id=view), key=lambda t: priority_rank(t.priority))
    for t in tickets:  # the CEO view still wants a name for runs whose record is long gone
        _assignee_name_cache[t.key] = t.assignee_name
    return [
        {"key": t.key, "summary": t.summary, "status": t.status, "type": t.issue_type, "url": t.url,
         "assignee": t.assignee_name, "priority": t.priority, "open_pr": _open_pr_for(t, config)}
        for t in tickets
    ]


@app.post("/api/tickets", status_code=201)
def create_ticket(body: TicketRequest) -> dict:
    if not _PROJECT_RE.match(body.project):
        raise HTTPException(status_code=422, detail="invalid project key")
    ticket = JiraClient(_get_config().jira).create_issue(body.project, body.summary.strip(), body.description)
    return {"key": ticket.key, "summary": ticket.summary, "url": ticket.url}


@app.post("/api/run", status_code=202)
def start_run(body: RunRequest, request: Request) -> dict:
    if not _TICKET_RE.match(body.ticket_id):
        raise HTTPException(status_code=422, detail="invalid ticket id")
    config = _get_config()
    actor, _source = _actor(request, body.actor)
    audit.record_access(endpoint="/api/run", ip=_client_ip(request), name=actor,
                        ticket_id=body.ticket_id, dry_run=body.dry_run)

    record = RunRecord(
        run_id=str(uuid.uuid4()),
        ticket_id=body.ticket_id,
        dry_run=body.dry_run,
        started_at=datetime.now(timezone.utc).isoformat(),
        status="queued",
        actor=actor,
    )
    record.person, record.assignee_name = _person_of(body.ticket_id)
    if record.assignee_name:
        _assignee_name_cache[body.ticket_id] = record.assignee_name
    with _runs_lock:
        _runs[record.run_id] = record

    queued = {"flag": False}  # set once submit() says "wait"

    def start() -> None:
        with record._lock:
            record.status = "running"
        if queued["flag"]:
            record.add_event({"type": "note", "message": "Warteschlange: der vorige Lauf ist beendet, dieser Lauf startet jetzt"})
        threading.Thread(target=_run_and_release, args=(record, config), daemon=True,
                         name=f"run-{record.ticket_id}").start()

    position = _queue.submit(record.person, record.run_id, start)
    if not position:
        with record._lock:
            if record.status == "queued":  # started synchronously; status is set in start()
                record.status = "running"
    if position:
        queued["flag"] = True
        record.add_event({"type": "note", "message": f"In der Warteschlange, Position {position}: für diese Person läuft "
                                                     "bereits ein Lauf. Dieser startet automatisch danach."})
        logger.info("run %s for %s queued at position %d (person %s)", record.run_id, record.ticket_id, position, record.person)
    return {"run_id": record.run_id, "queued": bool(position), "position": position}


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


def apply_decision(record: RunRecord, *, approved: bool, reason: str, actor: str,
                   actor_source: str, ip: str = "") -> None:
    """Release a waiting run and write the audit trail. Shared by the console and Telegram, so a
    tap in Telegram takes exactly the same path as the APPROVE button in the browser."""
    with record._lock:
        if record.status != "awaiting_approval":
            raise HTTPException(status_code=409, detail="run is not waiting for approval")
        record._approved = approved
        plan = record.plan or {}
    record._approval.set()
    # Audit after the decision took effect: logging must never delay or block the run.
    reason = reason if not approved else ""
    audit.record_approval(run_id=record.run_id, ticket_id=record.ticket_id,
                          decision="approved" if approved else "rejected",
                          actor=actor, actor_source=actor_source, risk_class=plan.get("risk_class", ""),
                          dry_run=record.dry_run, reason=reason, ip=ip)
    if not approved:
        audit.record_feedback("plan_rejected", ticket_id=record.ticket_id, run_id=record.run_id, plan=plan,
                              reason=reason or "(kein Grund angegeben)", minio_settings=_get_config().minio,
                              extra={"actor": actor, "actor_source": actor_source, "dry_run": record.dry_run})


@app.post("/api/run/{run_id}/approval")
def decide(run_id: str, body: ApprovalRequest, request: Request) -> dict:
    record = _get_run(run_id)
    actor, source = _actor(request, body.actor)
    apply_decision(record, approved=body.approved, reason=body.reason, actor=actor,
                   actor_source=source, ip=_client_ip(request))
    return {"approved": body.approved}


@app.get("/api/approvals")
def approvals(limit: int = 20) -> list[dict]:
    """The latest approvals, rejections and timeouts, newest first. IPs stay in the log file, not here."""
    rows = audit.read_jsonl(audit.APPROVAL_LOG, max(1, min(limit, 100)))
    return [{k: r.get(k) for k in ("ts", "ticket_id", "run_id", "decision", "actor", "actor_source",
                                   "risk_class", "dry_run", "reason")} for r in rows]


@app.get("/api/metrics")
def metrics() -> dict:
    """Aggregated from what already exists: the run log (every finished run since 30.09.2026) plus the
    console history (last 20). A starting point only; the "time saved" business dashboard is V1 work."""
    runs: dict[str, dict] = {}
    for r in list(_history) + audit.merged_runs():  # run log last: it has more fields
        if r.get("run_id"):
            runs[r["run_id"]] = {**runs.get(r["run_id"], {}), **r}
    rows = list(runs.values())
    by_status = {s: sum(1 for r in rows if r.get("status") == s) for s in ("done", "failed", "cancelled")}
    durations = [r["duration_seconds"] for r in rows if isinstance(r.get("duration_seconds"), (int, float))]
    full = [r for r in rows if not r.get("dry_run")]
    full_done = [r["duration_seconds"] for r in full if r.get("status") == "done" and isinstance(r.get("duration_seconds"), (int, float))]
    repair_known = [r for r in full if r.get("repair_attempts") is not None]
    judge_known = [r for r in full if r.get("judge_warning") is not None]
    return {
        "total_runs": len(rows),
        "by_status": by_status,
        "dry_runs": len(rows) - len(full),
        "full_runs": len(full),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "avg_duration_full_done_seconds": round(sum(full_done) / len(full_done), 1) if full_done else None,
        "needed_repair": sum(1 for r in repair_known if r["repair_attempts"]),
        "repair_known": len(repair_known),
        "judge_warnings": sum(1 for r in judge_known if r["judge_warning"]),
        "judge_reviewed": len(judge_known),
        "since": min((r.get("started_at") or "" for r in rows), default=None) or None,
        "load": _queue.counts(),
    }


@app.post("/api/voice-input")
async def voice_input(request: Request, purpose: Literal["ticket", "answer"] = "ticket") -> dict:
    """Raw audio body (webm/ogg/wav from the browser's MediaRecorder) -> Whisper.

    purpose=ticket: find the ticket and check it against Jira (status found / not_open / not_found /
    no_number / nothing, plus the open tickets to offer). purpose=answer: only ja/nein, no Jira call.
    """
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=422, detail="empty audio")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    content_type = request.headers.get("content-type", "audio/webm").split(";")[0].strip()
    extension = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/mp4": "m4a"}.get(content_type, "webm")

    # Imported here: jarvis.steps.voice_input pulls in the audio stack, which the console does not need otherwise.
    from jarvis.steps.voice_input import parse_yes_no, transcribe_audio

    config = _get_config()
    try:
        transcript = await asyncio.to_thread(
            transcribe_audio, audio, config, filename=f"audio.{extension}", content_type=content_type
        )
    except Exception as exc:
        logger.warning("voice transcription failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Whisper not reachable: {type(exc).__name__}") from exc

    # answer: True = ja, False = nein, None = unclear or silence (never treated as approval).
    body: dict[str, Any] = {"transcript": transcript, "answer": parse_yes_no(transcript)}
    if purpose == "answer":
        logger.info("voice answer: heard=%r answer=%s", transcript, body["answer"])
        return body

    from jarvis.steps.ticket_speech import resolve_spoken_ticket

    result = await asyncio.to_thread(resolve_spoken_ticket, transcript, JiraClient(config.jira))
    body.update(
        success=result.ticket_id is not None,
        ticket_id=result.ticket_id,
        status=result.status,
        project=result.project,
        candidate=result.candidate,
        summary=result.summary,
        say=result.say,
        open_tickets=result.open_tickets,
        log=result.log_line(),
    )
    return body


_MAX_WAKE_CLIP_BYTES = 1024 * 1024   # the browser sends at most ~6 s of 16 kHz WAV
_wake_lock = asyncio.Lock()
# The wake phrase only needs a small, fast model: tiny answers in ~0.4 s, large-v3 needs ~6 s on CAESAR.
_WAKE_MODEL = os.environ.get("WAKE_WHISPER_MODEL", "Systran/faster-whisper-tiny")
_WAKE_KEEP_WARM_SECONDS = 240         # the Whisper server unloads idle models; reloading tiny takes ~10 s
_WAKE_KEEP_WARM_FOR = 30 * 60         # ...but only while someone actually uses the wake word
_wake_last_used = 0.0
_wake_warm_task: asyncio.Task | None = None


async def _keep_wake_model_warm() -> None:
    from jarvis.steps.voice_input import transcribe_audio

    silence = _SILENT_WAV
    while time.monotonic() - _wake_last_used < _WAKE_KEEP_WARM_FOR:
        await asyncio.sleep(_WAKE_KEEP_WARM_SECONDS)
        try:
            await asyncio.to_thread(transcribe_audio, silence, _get_config(), filename="warm.wav", model=_WAKE_MODEL)
        except Exception as exc:
            logger.info("wake model keep-warm failed: %s", type(exc).__name__)


def _silent_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


_SILENT_WAV = _silent_wav()


@app.post("/api/wake-check")
async def wake_check(request: Request) -> dict:
    """Is this short clip the wake phrase ("Hallo JARVIS")? The browser only sends a clip when it hears
    speech, never a continuous stream. A command in the same breath is resolved to a ticket right away."""
    if _wake_lock.locked():  # one check at a time: chatter must not queue up Whisper calls
        raise HTTPException(status_code=429, detail="wake check busy")
    audio = await request.body()
    if not audio or len(audio) > _MAX_WAKE_CLIP_BYTES:
        raise HTTPException(status_code=413 if audio else 422, detail="clip empty or too long")
    from jarvis.steps.voice_input import transcribe_audio
    from jarvis.steps.wake_word import find_wake_phrase

    global _wake_last_used, _wake_warm_task
    config = _get_config()
    _wake_last_used = time.monotonic()
    if _wake_warm_task is None or _wake_warm_task.done():
        _wake_warm_task = asyncio.create_task(_keep_wake_model_warm())
    async with _wake_lock:
        try:
            heard = await asyncio.to_thread(
                transcribe_audio, audio, config, filename="wake.wav", content_type="audio/wav", model=_WAKE_MODEL
            )
        except Exception as exc:
            logger.warning("wake check: transcription failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Whisper not reachable: {type(exc).__name__}") from exc
    wake = find_wake_phrase(heard)
    accurate_done = False
    if not wake.woke and wake.maybe:
        # tiny heard a greeting and something name-like ("Hallo, JavaScript ..."): the accurate model decides
        try:
            second = await asyncio.to_thread(transcribe_audio, audio, config, filename="wake.wav", content_type="audio/wav")
            logger.info("wake check: second opinion for %r -> %r", heard, second)
            heard, wake, accurate_done = second, find_wake_phrase(second), True
        except Exception as exc:
            logger.info("wake check: second opinion failed (%s)", type(exc).__name__)
    body: dict[str, Any] = {"wake": wake.woke, "heard": heard, "command": wake.command}
    logger.info("wake check: heard=%r wake=%s command=%r model=%s", heard, wake.woke, wake.command, _WAKE_MODEL)
    if wake.woke and len(wake.command.split()) >= 2:
        # A command in the same breath: ticket numbers need the accurate model, so transcribe the clip again.
        from jarvis.steps.ticket_speech import resolve_spoken_ticket

        command = wake.command
        if not accurate_done:
            try:
                accurate = await asyncio.to_thread(transcribe_audio, audio, config, filename="wake.wav", content_type="audio/wav")
                again = find_wake_phrase(accurate)
                heard, command = accurate, (again.command if again.woke else accurate)
            except Exception as exc:
                logger.info("wake check: accurate transcription failed (%s), using the fast one", type(exc).__name__)
        body.update(heard=heard, command=command)
        result = await asyncio.to_thread(resolve_spoken_ticket, command, JiraClient(config.jira))
        body["ticket"] = {
            "transcript": heard, "status": result.status, "ticket_id": result.ticket_id, "candidate": result.candidate,
            "project": result.project, "summary": result.summary, "say": result.say,
            "open_tickets": result.open_tickets, "log": result.log_line(),
        }
    return body


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
def recent_runs(request: Request, limit: int = 5, assignee: str = "") -> list[dict]:
    limit = max(1, min(limit, _HISTORY_LIMIT))
    view = _view_as(request, assignee)
    if view is None:
        return _history[:limit]
    if view == "-":
        return []
    keys = sorted({h["ticket_id"] for h in _history if _TICKET_RE.match(h.get("ticket_id", ""))})
    stale = [k for k in keys if time.monotonic() - _assignee_cache.get(k, (0.0, ""))[0] > 60]
    if stale:
        try:
            for k, a in JiraClient(_get_config().jira).assignees_of(stale).items():
                _assignee_cache[k] = (time.monotonic(), a)
        except Exception as exc:
            logger.warning("assignee lookup for history failed: %s", type(exc).__name__)
    return [h for h in _history if _assignee_cache.get(h["ticket_id"], (0.0, ""))[1] == view][:limit]


@app.websocket("/ws/{run_id}")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    mode, _ = await asyncio.to_thread(auth_mode)
    if mode == "keycloak" and current_user(websocket) is None:
        await websocket.close(code=1008)
        return
    record = _runs.get(run_id)
    if record is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    sent = 0
    quiet_after_finish = 0.0
    try:
        while True:
            events, finished = record.snapshot(since=sent)
            for event in events:
                await websocket.send_json(event)
            sent += len(events)
            if finished:
                # The self-rating is computed after the run ends, so stay a little longer and
                # let that note reach the live log instead of only appearing on a reload.
                quiet_after_finish = 0.0 if events else quiet_after_finish + 0.25
                if quiet_after_finish >= _WS_LINGER_SECONDS:
                    break
            await asyncio.sleep(0.25)
        await websocket.close()
    except WebSocketDisconnect:
        return
