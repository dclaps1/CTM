"""Pull calls and agents from CallTrackingMetrics into the local database."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app import db
from app.ctm_client import CTMClient, CTMError
from app.models import Agent, Call, SyncState, utcnow

log = logging.getLogger(__name__)

# CTM's activity feed also contains texts, forms and chats; we only keep voice calls.
NON_CALL_MARKERS = ("msg", "sms", "form", "chat", "email")
ANSWERED_STATUSES = {"answered", "completed"}
CALLED_AT_FORMATS = ("%Y-%m-%d %I:%M %p %z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M:%S")
# Re-pull this many days before the cursor so late edits (notes, tags, scores, transcripts) land.
RESYNC_OVERLAP_DAYS = 2
COMMIT_EVERY = 200

_sync_lock = threading.Lock()


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_called_at(raw: dict[str, Any]) -> datetime | None:
    unix = raw.get("unix_time")
    if unix not in (None, ""):
        try:
            return datetime.fromtimestamp(int(unix), tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    text = _str(raw.get("called_at"))
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in CALLED_AT_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed and parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _tags(raw: dict[str, Any]) -> list[str]:
    tags = raw.get("tag_list") or raw.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    out = []
    for tag in tags:
        name = tag.get("name") if isinstance(tag, dict) else tag
        name = _str(name).replace(",", " ")
        if name:
            out.append(name)
    return out


def normalize_call(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a CTM call JSON object onto Call columns. Returns None for non-call activity."""
    try:
        call_id = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    direction_raw = _str(raw.get("direction") or "inbound").lower()
    if any(marker in direction_raw for marker in NON_CALL_MARKERS):
        return None

    status = _str(raw.get("dial_status") or raw.get("call_status") or raw.get("status")).lower()
    duration = _int(raw.get("duration"))
    talk_time = _int(raw.get("talk_time"))
    voicemail = "voicemail" in status
    if not talk_time and status in ANSWERED_STATUSES:
        talk_time = duration
    answered = not voicemail and (talk_time > 0 or status in ANSWERED_STATUSES)

    agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    agent_id = _str(agent.get("id") or raw.get("agent_id")) or None
    sale = raw.get("sale") if isinstance(raw.get("sale"), dict) else {}
    sale_score = _int(sale.get("score")) or None
    sale_conversion = sale.get("conversion")
    try:
        sale_value = float(sale["value"]) if sale.get("value") not in (None, "") else None
    except (TypeError, ValueError):
        sale_value = None

    return {
        "id": call_id,
        "called_at": _parse_called_at(raw) or utcnow(),
        "direction": "outbound" if "outbound" in direction_raw else "inbound",
        "status": status,
        "answered": answered,
        "voicemail": voicemail,
        "duration": duration,
        "talk_time": talk_time,
        "ring_time": _int(raw.get("ring_time")),
        "caller_number": _str(raw.get("caller_number") or raw.get("caller_number_complete")),
        "caller_name": _str(raw.get("name") or raw.get("cnam")),
        "caller_city": _str(raw.get("city")),
        "caller_state": _str(raw.get("state")),
        "tracking_number": _str(raw.get("tracking_number")),
        "tracking_label": _str(raw.get("tracking_label")),
        "source": _str(raw.get("source")),
        "receiving_number": _str(raw.get("receiving_number") or raw.get("business_number")),
        "agent_id": agent_id,
        "agent_name": _str(agent.get("name")),
        "tags": ",".join(_tags(raw)),
        "is_new_caller": bool(raw.get("is_new_caller")),
        "sale_score": sale_score,
        "sale_conversion": None if sale_conversion in (None, "") else bool(_int(sale_conversion)),
        "sale_value": sale_value,
        "notes": _str(raw.get("notes")) or None,
        "transcript": _str(raw.get("transcription_text") or raw.get("transcript")) or None,
        "audio_url": _str(raw.get("audio") or raw.get("recording_url")) or None,
        "raw": raw,
    }


def upsert_agent(session: Session, agent_id: str, name: str = "", email: str | None = None) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        agent = Agent(id=agent_id, name=name or f"Agent {agent_id}", email=email or None)
        session.add(agent)
        session.flush()
        return agent
    if name:
        agent.name = name
    if email:
        agent.email = email
    return agent


def upsert_call(session: Session, raw: dict[str, Any]) -> Call | None:
    data = normalize_call(raw)
    if data is None:
        return None
    if data["agent_id"]:
        agent_raw = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
        upsert_agent(session, data["agent_id"], data["agent_name"], _str(agent_raw.get("email")) or None)
    call = session.get(Call, data["id"])
    if call is None:
        call = Call(id=data["id"])
        session.add(call)
    for key, value in data.items():
        setattr(call, key, value)
    return call


def sync_agents(session: Session, client: CTMClient) -> int:
    count = 0
    for raw in client.iter_users():
        agent_id = _str(raw.get("id"))
        if not agent_id:
            continue
        name = _str(raw.get("name")) or " ".join(
            p for p in (_str(raw.get("first_name")), _str(raw.get("last_name"))) if p
        )
        upsert_agent(session, agent_id, name, _str(raw.get("email")) or None)
        count += 1
    session.commit()
    return count


def sync_calls(session: Session, client: CTMClient, start: date, end: date) -> int:
    count = 0
    for raw in client.iter_calls(start, end):
        if upsert_call(session, raw) is not None:
            count += 1
            if count % COMMIT_EVERY == 0:
                session.commit()
    session.commit()
    return count


def get_state(session: Session, key: str) -> str:
    row = session.get(SyncState, key)
    return row.value if row else ""


def set_state(session: Session, key: str, value: str) -> None:
    row = session.get(SyncState, key)
    if row is None:
        session.add(SyncState(key=key, value=value))
    else:
        row.value = value


def run_sync(
    session: Session,
    client: CTMClient,
    *,
    initial_days: int = 30,
    days: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Incremental sync: agents, then calls from the stored cursor (minus overlap) through tomorrow."""
    if not _sync_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "a sync is already running"}
    try:
        today = today or utcnow().date()
        cursor = get_state(session, "calls_synced_through")
        if days is not None:
            start = today - timedelta(days=days)
        elif cursor:
            start = date.fromisoformat(cursor) - timedelta(days=RESYNC_OVERLAP_DAYS)
        else:
            start = today - timedelta(days=initial_days)
        end = today + timedelta(days=1)  # CTM dates are account-local; cover the UTC offset

        try:
            agents = sync_agents(session, client)
        except CTMError as exc:  # user listing may be restricted for the API key; calls still carry agents
            session.rollback()
            log.warning("Skipping agent sync: %s", exc)
            agents = 0
        calls = sync_calls(session, client, start, end)

        set_state(session, "calls_synced_through", today.isoformat())
        set_state(session, "last_sync_at", utcnow().isoformat(timespec="seconds"))
        set_state(session, "last_sync_result", f"{calls} calls, {agents} agents ({start} to {end})")
        set_state(session, "last_sync_error", "")
        session.commit()
        return {"status": "ok", "calls": calls, "agents": agents, "start": str(start), "end": str(end)}
    except Exception as exc:
        session.rollback()
        set_state(session, "last_sync_at", utcnow().isoformat(timespec="seconds"))
        set_state(session, "last_sync_error", str(exc)[:500])
        session.commit()
        raise
    finally:
        _sync_lock.release()


def sync_once(client_factory: Callable[[], CTMClient], initial_days: int, days: int | None = None) -> dict[str, Any]:
    session = db.new_session()
    try:
        with client_factory() as client:
            return run_sync(session, client, initial_days=initial_days, days=days)
    finally:
        session.close()


class SyncWorker:
    """Background thread that runs an incremental sync every `interval_minutes`."""

    def __init__(self, client_factory: Callable[[], CTMClient], interval_minutes: int, initial_days: int):
        self._client_factory = client_factory
        self._interval = max(1, interval_minutes) * 60
        self._initial_days = initial_days
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ctm-sync", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = sync_once(self._client_factory, self._initial_days)
                log.info("CTM sync: %s", result)
            except Exception:
                log.exception("CTM sync failed")
            self._stop.wait(self._interval)
