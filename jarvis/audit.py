"""Durable, append-only records around runs (V1 groundwork). Nothing here changes how a run behaves.

- approvals   .jarvis/approval_log.jsonl   who approved or rejected which plan, when, risk class, reason
- access      .jarvis/access_log.jsonl     every /api/run call: source IP + demo-selected name (no real auth yet)
- run log     .jarvis/run_log.jsonl        one line per finished run, the base for /api/metrics
- feedback    MinIO jarvis-artifacts/feedback/...  rejected plans and judge warnings, collected for later
              training or prompt tuning (no pipeline consumes it yet). Falls back to .jarvis/feedback/.

JSON lines are appended under a process lock; each line is small, so the console and a CLI run
writing at the same time do not corrupt each other. Every writer is best effort: logging a
record must never fail or slow down a run.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.audit")

_DIR = Path(__file__).resolve().parents[1] / ".jarvis"
APPROVAL_LOG = _DIR / "approval_log.jsonl"
ACCESS_LOG = _DIR / "access_log.jsonl"
RUN_LOG = _DIR / "run_log.jsonl"
FEEDBACK_DIR = _DIR / "feedback"
_MAX_REASON_CHARS = 500
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(path: Path, record: dict) -> None:
    try:
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        logger.exception("audit: could not write %s", path.name)


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    """Newest first. Broken lines (e.g. a crash mid-write) are skipped."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if limit and len(out) >= limit:
            break
    return out


def plan_dict(plan: Any) -> dict:
    """A Plan object or the console's plan view, as plain data."""
    if plan is None:
        return {}
    if isinstance(plan, dict):
        return {k: plan.get(k) for k in ("approach", "files", "test_plan", "risk_class")}
    return {
        "approach": plan.approach,
        "files": [{"path": fc.path, "reason": fc.reason} for fc in plan.files_to_change],
        "test_plan": list(plan.test_plan),
        "risk_class": plan.risk_class,
    }


# ---- approvals ------------------------------------------------------------------------------

def record_approval(*, run_id: str, ticket_id: str, decision: str, actor: str, actor_source: str,
                    risk_class: str, dry_run: bool, reason: str = "", ip: str = "") -> dict:
    """decision: approved | rejected | timeout. actor_source says how much the name can be trusted:
    "demo-auswahl" (picked in the demo dropdown, not verified), "keycloak", "cli" (OS user), "system"."""
    record = {
        "ts": now_iso(), "run_id": run_id, "ticket_id": ticket_id, "decision": decision,
        "actor": actor or "unbekannt", "actor_source": actor_source, "risk_class": risk_class,
        "dry_run": dry_run, "reason": (reason or "").strip()[:_MAX_REASON_CHARS], "ip": ip,
    }
    _append(APPROVAL_LOG, record)
    logger.info("approval: %s %s by %s (%s), risk=%s", ticket_id, decision, record["actor"], actor_source, risk_class)
    return record


# ---- access log (stopgap while the console has no login) ------------------------------------

ACCESS_NOTE = "no real auth, IP + demo-selected name only"


def record_access(*, endpoint: str, ip: str, name: str, ticket_id: str, dry_run: bool) -> None:
    logger.info("ACCESS %s (%s): ip=%s name=%s ticket=%s dry_run=%s",
                endpoint, ACCESS_NOTE, ip or "?", name or "-", ticket_id, dry_run)
    _append(ACCESS_LOG, {"ts": now_iso(), "endpoint": endpoint, "ip": ip, "name": name,
                         "ticket_id": ticket_id, "dry_run": dry_run, "note": ACCESS_NOTE})


# ---- run log (metrics) ----------------------------------------------------------------------

def record_run(entry: dict) -> None:
    _append(RUN_LOG, {"logged_at": now_iso(), **entry})


def record_self_rating(run_id: str, ticket_id: str, rating: dict) -> None:
    """Appended after the run is already logged, because the rating is computed afterwards.
    Readers merge every line of a run_id, so this fills in the field without rewriting anything."""
    _append(RUN_LOG, {"logged_at": now_iso(), "run_id": run_id, "ticket_id": ticket_id,
                      "self_rating": rating})


def merged_runs() -> list[dict]:
    """One entry per run, newest first, with later lines filled in over earlier ones."""
    merged: dict[str, dict] = {}
    for entry in reversed(read_jsonl(RUN_LOG)):        # read_jsonl is newest first, so go oldest first
        run_id = entry.get("run_id")
        if run_id:
            merged.setdefault(run_id, {}).update(entry)
    return sorted(merged.values(), key=lambda r: r.get("started_at") or r.get("logged_at") or "",
                  reverse=True)


# ---- feedback (data collection only) --------------------------------------------------------

def record_feedback(kind: str, *, ticket_id: str, run_id: str, plan: Any, reason: str,
                    minio_settings: Any = None, extra: dict | None = None) -> None:
    """kind: plan_rejected | judge_warning. Written in a background thread so it never delays a run
    or an approval response; not a daemon, so a CLI process waits for it before exiting."""
    record = {"ts": now_iso(), "kind": kind, "ticket_id": ticket_id, "run_id": run_id,
              "plan": plan_dict(plan), "reason": (reason or "")[:4000], **(extra or {})}
    name = f"{kind}-{ticket_id}-{run_id[:8]}.json"
    day = record["ts"][:10]
    threading.Thread(target=_store_feedback, args=(record, day, name, minio_settings),
                     name=f"feedback-{ticket_id}").start()


def _store_feedback(record: dict, day: str, name: str, minio_settings: Any) -> None:
    body = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    if minio_settings is not None:
        try:
            from jarvis.clients.minio_client import MinioClient

            key = MinioClient(minio_settings).save_object(f"feedback/{day}/{name}", body, "application/json")
            logger.info("feedback: stored %s", key)
            return
        except Exception as exc:
            logger.warning("feedback: MinIO not available (%s), keeping it locally", type(exc).__name__)
    try:
        path = FEEDBACK_DIR / day / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        logger.info("feedback: stored locally %s", path)
    except OSError:
        logger.exception("feedback: could not store %s", name)
