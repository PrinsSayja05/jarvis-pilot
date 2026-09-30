"""Approve or reject a plan from Telegram.

Polling, not a webhook. A webhook needs Telegram's servers to open a connection to us, and
SOKRATES-1 only has a private address (192.168.178.75) behind NAT, so Telegram cannot reach it
at all. The self-signed certificate is not the blocker (Telegram accepts an uploaded self-signed
cert); reachability is. Polling works from behind NAT, opens no inbound port, and needs no
tunnel. If cloudflared is ever set up (WMCNL-2544) a webhook becomes possible, but polling stays
the safer default because it exposes nothing.

Who may press a button: a Telegram account is not a Jira account, so the two are matched through
.jarvis/telegram_users.json, a plain map of Telegram user id to Jira account id. The file is read
again on every press, so it can be edited without restarting the service. Anyone who is not the
ticket's assignee is refused. An unmapped user is refused and told their own Telegram id, which
is what makes the file easy to fill in the first place.

High risk never gets approved by one tap: that rule already holds for the voice path and for
--auto-approve, and it holds here too. The button answers with a link to the console instead.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from jarvis.config import JarvisConfig
from jarvis.steps.notify import APPROVE_DATA, CONSOLE_URL, PLAN_MESSAGES, REJECT_DATA

logger = logging.getLogger("jarvis.telegram")

USER_MAP_FILE = Path(__file__).resolve().parents[2] / ".jarvis" / "telegram_users.json"
REASON_WINDOW_SECONDS = 30
_POLL_TIMEOUT = 25
_HIGH_RISK = "high"

# (chat_id, telegram user id) -> {"event": Event, "text": str | None}
_pending_reason: dict[tuple[int, int], dict] = {}
_pending_lock = threading.Lock()
_started = threading.Lock()


def load_user_map() -> dict[str, dict]:
    """{telegram user id (str): {"jira": accountId, "name": str}}. Missing or broken file = nobody mapped."""
    try:
        raw = json.loads(USER_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if isinstance(value, str):
            value = {"jira": value, "name": ""}
        if isinstance(value, dict) and value.get("jira"):
            out[str(key)] = {"jira": value["jira"], "name": value.get("name", "")}
    return out


class TelegramBot:
    def __init__(self, config: JarvisConfig, resolve_run: Callable[[str], Any],
                 decide: Callable[..., None]) -> None:
        self._token = config.telegram.bot_token
        self._chat_id = str(config.telegram.engineer_chat_id)
        self._config = config
        self._resolve_run = resolve_run
        self._decide = decide
        self._offset = 0

    # ---- Telegram API ----------------------------------------------------------------------

    def _api(self, method: str, **payload) -> dict | None:
        """Never logs the URL: it carries the bot token."""
        try:
            with httpx.Client(timeout=_POLL_TIMEOUT + 15) as client:
                r = client.post(f"https://api.telegram.org/bot{self._token}/{method}", json=payload)
            if r.status_code == 409:
                logger.error("telegram %s: 409 Conflict, another poller or a webhook is active", method)
                return None
            r.raise_for_status()
            return r.json().get("result")
        except httpx.HTTPStatusError as exc:
            logger.warning("telegram %s failed: HTTP %s", method, exc.response.status_code)
        except httpx.HTTPError as exc:
            logger.warning("telegram %s failed: %s", method, type(exc).__name__)
        return None

    def _answer(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self._api("answerCallbackQuery", callback_query_id=callback_id, text=text[:200], show_alert=alert)

    def _say(self, text: str, reply_to: int | None = None, force_reply: bool = False) -> int | None:
        payload: dict = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        if force_reply:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        result = self._api("sendMessage", **payload)
        return (result or {}).get("message_id")

    def _close_plan_message(self, run_id: str, footer: str) -> None:
        """Replace the plan message's buttons with the decision that was taken."""
        where = PLAN_MESSAGES.pop(run_id, None)
        if not where:
            return
        chat_id, message_id, body = where
        self._api("editMessageText", chat_id=chat_id, message_id=message_id,
                  text=(body + "\n\n" + footer).strip(), parse_mode="HTML",
                  disable_web_page_preview=True, reply_markup={"inline_keyboard": []})

    # ---- polling ---------------------------------------------------------------------------

    def run_forever(self) -> None:
        logger.info("telegram: polling for approvals (chat %s, %d user(s) mapped)",
                    self._chat_id, len(load_user_map()))
        ready = False
        while True:
            try:
                updates = self._api("getUpdates", offset=self._offset, timeout=_POLL_TIMEOUT,
                                    allowed_updates=["callback_query", "message"])
                if updates is None:  # a failed call, e.g. 409 while an old instance still holds the poll
                    time.sleep(5)    # never hammer Telegram in a tight loop
                    continue
                if not ready:
                    ready = True
                    logger.info("telegram: connected, waiting for button presses")
                for update in updates:
                    self._offset = max(self._offset, update.get("update_id", 0) + 1)
                    try:
                        self._dispatch(update)
                    except Exception:
                        logger.exception("telegram: could not handle update")
            except Exception:
                logger.exception("telegram: polling error, retrying in 5s")
                time.sleep(5)

    def _dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_button(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _on_message(self, message: dict) -> None:
        """Only interesting while a rejection is waiting for its reason."""
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        text = (message.get("text") or "").strip()
        if not text:
            return
        with _pending_lock:
            waiting = _pending_reason.get((chat_id, user_id))
        if waiting is not None:
            waiting["text"] = text
            waiting["event"].set()

    # ---- the button ------------------------------------------------------------------------

    def _on_button(self, query: dict) -> None:
        data = query.get("data") or ""
        callback_id = query["id"]
        user = query.get("from", {})
        user_id = user.get("id")
        display = user.get("first_name") or user.get("username") or str(user_id)

        if data.startswith(APPROVE_DATA):
            approved, run_id = True, data[len(APPROVE_DATA):]
        elif data.startswith(REJECT_DATA):
            approved, run_id = False, data[len(REJECT_DATA):]
        else:
            return

        record = self._resolve_run(run_id)
        if record is None:
            self._answer(callback_id, "Dieser Lauf ist nicht mehr bekannt. Bitte in der Konsole nachsehen.", True)
            return
        if record.status != "awaiting_approval":
            self._answer(callback_id, "Dieser Lauf wartet nicht mehr auf eine Freigabe.", True)
            return

        allowed, name, why = self._may_decide(record, user_id, display)
        if not allowed:
            self._answer(callback_id, why, True)
            logger.warning("telegram: %s (id %s) may not decide %s", display, user_id, record.ticket_id)
            return

        risk = (record.plan or {}).get("risk_class", "")
        if approved and risk == _HIGH_RISK:
            self._answer(callback_id, "Hohes Risiko: bitte in der Konsole bestätigen.", True)
            self._say(f"⚠️ <b>Hohes Risiko</b> — {record.ticket_id}\nBitte in der Konsole bestätigen: {CONSOLE_URL}")
            logger.info("telegram: high-risk approval for %s refused, console confirmation required", record.ticket_id)
            return

        if approved:
            self._finish(record, run_id, True, "", name, callback_id)
        else:
            self._answer(callback_id, "Abgelehnt. Grund? Antwort innerhalb von 30 Sekunden.")
            threading.Thread(target=self._reject_with_reason, args=(record, run_id, name, user_id),
                             name=f"tg-reason-{record.ticket_id}", daemon=True).start()

    def _may_decide(self, record, user_id: int, display: str) -> tuple[bool, str, str]:
        mapping = load_user_map()
        me = mapping.get(str(user_id))
        assignee = getattr(record, "assignee_name", "") or "die zuständige Person"
        if me is None:
            return False, display, (f"Unbekanntes Telegram-Konto. Deine ID ist {user_id}. "
                                    f"Bitte in .jarvis/telegram_users.json eintragen.")
        if record.person and record.person != "unassigned" and me["jira"] != record.person:
            return False, me.get("name") or display, f"Nur {assignee} kann dieses Ticket freigeben."
        return True, me.get("name") or display, ""

    def _reject_with_reason(self, record, run_id: str, name: str, user_id: int) -> None:
        key = (int(self._chat_id), user_id)
        waiting = {"event": threading.Event(), "text": None}
        with _pending_lock:
            _pending_reason[key] = waiting
        prompt = self._say(f"❌ {record.ticket_id} abgelehnt von {name}.\n"
                           f"Grund? Bitte innerhalb von {REASON_WINDOW_SECONDS} Sekunden antworten.",
                           force_reply=True)
        waiting["event"].wait(REASON_WINDOW_SECONDS)
        with _pending_lock:
            _pending_reason.pop(key, None)
        reason = (waiting["text"] or "").strip()
        if not reason:
            self._say("Kein Grund angegeben, die Ablehnung wird so vermerkt.", reply_to=prompt)
        self._finish(record, run_id, False, reason, name, None)

    def _finish(self, record, run_id: str, approved: bool, reason: str, name: str,
                callback_id: str | None) -> None:
        try:
            self._decide(record, approved=approved, reason=reason, actor=name,
                         actor_source="telegram", ip="")
        except Exception:
            logger.exception("telegram: decision for %s could not be applied", record.ticket_id)
            if callback_id:
                self._answer(callback_id, "Die Freigabe konnte nicht angewendet werden.", True)
            return
        when = datetime.now().strftime("%H:%M")
        if approved:
            footer = f"✅ <b>Freigegeben</b> von {name} um {when}"
        else:
            footer = f"❌ <b>Abgelehnt</b> von {name} um {when}\nGrund: {reason or 'kein Grund angegeben'}"
        self._close_plan_message(run_id, footer)
        if callback_id:
            self._answer(callback_id, "Freigegeben." if approved else "Abgelehnt.")
        logger.info("telegram: %s %s by %s", record.ticket_id, "approved" if approved else "rejected", name)


def start(config: JarvisConfig, resolve_run: Callable[[str], Any], decide: Callable[..., None]) -> bool:
    """Start the poller once. Returns False when Telegram is not configured."""
    if config.telegram.bot_token in ("", "disabled"):
        logger.info("telegram: approvals from Telegram are off, no bot token configured")
        return False
    if not _started.acquire(blocking=False):
        return False
    bot = TelegramBot(config, resolve_run, decide)
    threading.Thread(target=bot.run_forever, name="telegram-poller", daemon=True).start()
    return True
